#![cfg(feature = "tpg366")]

use esibd_native_worker::{
    Backend,
    context::Context,
    tpg366::{Controller, GUI_ATTRIBUTES, GUI_METHODS, Link, Port, parse_pressures},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    io::{self, Read, Write},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

const ID: &str = "TPG366,PTG28770,44990000,010300,010100";
const GAUGES: &str = "TPR/PCR,IKR,PKR,CMR,IMR,PBR";
const FRAME: &str = "0,1.2345E-01,0,2.2345E-02,0,3.2345E-03,0,4.2345E-04,0,5.2345E-05,0,6.2345E-06";

struct Instrument {
    input: VecDeque<u8>,
    writes: Vec<Vec<u8>>,
    command: String,
    awaiting_enq: bool,
    responses: BTreeMap<String, Vec<u8>>,
    acknowledgements: VecDeque<Vec<u8>>,
    before_ack: Vec<u8>,
    flush_tail: Vec<u8>,
    clears: usize,
    flushes: usize,
    short_write: Option<Vec<u8>>,
    failed_write: Option<Vec<u8>>,
    read_error: Option<io::ErrorKind>,
    cancel_after_etx: Option<Context>,
    cancel_after_ack: Option<Context>,
}

impl Default for Instrument {
    fn default() -> Self {
        Self {
            input: VecDeque::new(),
            writes: Vec::new(),
            command: String::new(),
            awaiting_enq: false,
            responses: [("AYT", ID), ("TID", GAUGES), ("UNI", "4"), ("PRX", FRAME)]
                .map(|(key, value)| (key.to_string(), format!("{value}\r\n").into_bytes()))
                .into_iter()
                .collect(),
            acknowledgements: VecDeque::new(),
            before_ack: Vec::new(),
            flush_tail: Vec::new(),
            clears: 0,
            flushes: 0,
            short_write: None,
            failed_write: None,
            read_error: None,
            cancel_after_etx: None,
            cancel_after_ack: None,
        }
    }
}

struct Fake(Arc<Mutex<Instrument>>);
impl Read for Fake {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        let mut instrument = self.0.lock().unwrap();
        if let Some(kind) = instrument.read_error {
            return Err(kind.into());
        }
        if instrument.input.len() == 1
            && instrument.input.front() == Some(&b'\n')
            && let Some(context) = instrument.cancel_after_ack.take()
        {
            context.cancel();
        }
        match instrument.input.pop_front() {
            Some(byte) => {
                bytes[0] = byte;
                Ok(1)
            }
            None => {
                drop(instrument);
                std::thread::sleep(Duration::from_millis(1));
                Err(io::ErrorKind::TimedOut.into())
            }
        }
    }
}
impl Write for Fake {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let mut instrument = self.0.lock().unwrap();
        instrument.writes.push(bytes.to_vec());
        if instrument.failed_write.as_deref() == Some(bytes) {
            return Err(io::Error::other("USB transport failed"));
        }
        if instrument.short_write.as_deref() == Some(bytes) {
            return Ok(bytes.len() - 1);
        }
        match bytes {
            [3] => {
                instrument.awaiting_enq = false;
                if let Some(context) = instrument.cancel_after_etx.take() {
                    context.cancel();
                }
            }
            [5] => {
                assert!(instrument.awaiting_enq, "ENQ without a pending mnemonic");
                instrument.awaiting_enq = false;
                let response = instrument.responses[&instrument.command].clone();
                instrument.input.extend(response);
            }
            _ => {
                assert!(bytes.ends_with(b"\r") && !bytes.contains(&b'\n'));
                assert!(
                    !instrument.awaiting_enq,
                    "A second command interrupted the handshake"
                );
                instrument.command = String::from_utf8(bytes[..bytes.len() - 1].to_vec()).unwrap();
                assert!(
                    instrument.responses.contains_key(&instrument.command),
                    "Non-read-only command"
                );
                let ack = instrument
                    .acknowledgements
                    .pop_front()
                    .unwrap_or_else(|| b"\x06\r\n".to_vec());
                instrument.awaiting_enq = !ack.starts_with(&[21]);
                let tail = std::mem::take(&mut instrument.before_ack);
                instrument.input.extend(tail);
                instrument.input.extend(ack);
            }
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        let mut instrument = self.0.lock().unwrap();
        instrument.flushes += 1;
        let tail = std::mem::take(&mut instrument.flush_tail);
        instrument.input.extend(tail);
        Ok(())
    }
}
impl Port for Fake {
    fn clear_input(&mut self) -> io::Result<()> {
        let mut instrument = self.0.lock().unwrap();
        instrument.clears += 1;
        instrument.input.clear();
        Ok(())
    }
}

fn link() -> (Link, Arc<Mutex<Instrument>>) {
    let instrument = Arc::new(Mutex::new(Instrument::default()));
    (
        Link::new(
            Box::new(Fake(instrument.clone())),
            Duration::from_millis(20),
            Duration::ZERO,
        ),
        instrument,
    )
}
fn pressures(value: &Value) -> &[Value] {
    value["pressures"]["$tuple"].as_array().unwrap()
}
fn writes(instrument: &Arc<Mutex<Instrument>>) -> Vec<Vec<u8>> {
    instrument.lock().unwrap().writes.clone()
}

#[test]
fn full_handshake_and_poll_use_enq_alone_and_no_trailing_nak_or_settings_write() {
    let (mut link, instrument) = link();
    link.initialize(&Context::test(1.0)).unwrap();
    let reading = link.read_pressures(&Context::test(1.0)).unwrap();
    assert_eq!(link.identification, ID);
    assert_eq!(link.gauges.len(), 6);
    assert_eq!(reading["unit"], json!("hPa"));
    assert_eq!(pressures(&reading)[0], json!(0.12345));
    assert_eq!(
        writes(&instrument),
        [
            b"\x03".to_vec(),
            b"AYT\r".to_vec(),
            vec![5],
            b"TID\r".to_vec(),
            vec![5],
            b"UNI\r".to_vec(),
            vec![5],
            b"PRX\r".to_vec(),
            vec![5],
            b"UNI\r".to_vec(),
            vec![5]
        ]
    );
    instrument.lock().unwrap().writes.clear();
    link.read_pressures(&Context::test(1.0)).unwrap();
    assert_eq!(
        writes(&instrument),
        [b"PRX\r".to_vec(), vec![5], b"UNI\r".to_vec(), vec![5]]
    );
}

#[test]
fn documented_units_negative_offsets_and_all_status_sentinels_preserve_validity() {
    let frame = "0,-1,1,1,2,2,3,3,4,4,6,6";
    for (unit, factor, label) in [
        (0, 1.0, "mbar"),
        (1, 1013.25 / 760.0, "Torr"),
        (2, 0.01, "Pa"),
        (3, 1013.25 / 760000.0, "Micron"),
        (4, 1.0, "hPa"),
    ] {
        let reading = parse_pressures(frame, unit, 42.0).unwrap();
        assert_eq!(pressures(&reading)[0], json!(-factor));
        assert!(
            pressures(&reading)[1..]
                .iter()
                .all(|value| *value == json!({"$float":"nan"}))
        );
        assert_eq!(reading["unit"], json!(label));
    }
    let reading = parse_pressures("5,9,0,1,0,1,0,1,0,1,0,1", 0, 0.0).unwrap();
    assert_eq!(pressures(&reading)[0], json!({"$float":"nan"}));
    assert_eq!(reading["statuses"]["$tuple"][0], json!(5));
}

#[test]
fn malformed_frames_nonfinite_numbers_statuses_units_and_timestamps_are_not_samples() {
    for bad in [
        "0,1E-3".into(),
        format!("{FRAME},0,1"),
        FRAME.replacen("0,", "7,", 1),
        FRAME.replace("1.2345E-01", "nan"),
        FRAME.replace("1.2345E-01", "inf"),
        FRAME.replace("1.2345E-01", "1E999"),
        FRAME.replace("1.2345E-01", "bad"),
        FRAME.replacen("0,", "00,", 1),
        FRAME.replacen("0,", "-1,", 1),
    ] {
        assert_eq!(
            parse_pressures(&bad, 0, 0.0).unwrap_err().kind,
            "ProtocolError"
        );
    }
    assert!(parse_pressures(FRAME, 5, 0.0).is_err());
    assert!(parse_pressures(FRAME, 0, f64::NAN).is_err());
}

#[test]
fn startup_accepts_every_fragmented_stale_tail_but_normal_queries_never_discard_frames() {
    let stream = format!("{FRAME}\r\n").into_bytes();
    for offset in 0..=stream.len() {
        let (mut link, instrument) = link();
        instrument.lock().unwrap().before_ack = stream[offset..].to_vec();
        link.initialize(&Context::test(1.0)).unwrap();
        assert_eq!(
            writes(&instrument)
                .iter()
                .filter(|bytes| bytes.as_slice() == b"AYT\r")
                .count(),
            1
        );
    }
    let (mut link, instrument) = link();
    instrument.lock().unwrap().before_ack = b"unsolicited\r\n".to_vec();
    assert!(
        link.query("UNI", false, &Context::test(1.0))
            .unwrap_err()
            .message
            .contains("waiting for ACK")
    );
    assert_eq!(writes(&instrument), [b"UNI\r".to_vec()]);
}

#[test]
fn incomplete_and_corrupt_ack_never_cause_enq_or_implicit_retry() {
    for ack in [
        &b""[..],
        b"\x06",
        b"\x06\r",
        b"\x06\n",
        b"\x06X\r\n",
        b"junk\x06\r\n",
        b"\x06\x06\r\n",
        b"\x15\x06\r\n",
        b"\0\x06\r\n",
        b"\x06\n\r\n",
    ] {
        let (mut link, instrument) = link();
        instrument
            .lock()
            .unwrap()
            .acknowledgements
            .push_back(ack.to_vec());
        let error = link.query("AYT", true, &Context::test(1.0)).unwrap_err();
        assert!(error.message.contains("AYT [waiting for ACK]"));
        assert_eq!(writes(&instrument), [b"AYT\r".to_vec()]);
    }
}

#[test]
fn only_nak_is_retransmitted_and_three_transmissions_are_a_terminal_error() {
    let (mut link, instrument) = link();
    instrument
        .lock()
        .unwrap()
        .acknowledgements
        .extend([b"\x15\r\n".to_vec(), b"\x15\r\n".to_vec()]);
    assert_eq!(link.query("UNI", false, &Context::test(1.0)).unwrap(), "4");
    assert_eq!(link.nak_count, 2);
    assert_eq!(
        writes(&instrument),
        [
            b"UNI\r".to_vec(),
            b"UNI\r".to_vec(),
            b"UNI\r".to_vec(),
            vec![5]
        ]
    );
    let (mut link, instrument) = self::link();
    instrument.lock().unwrap().before_ack = b"\n".to_vec();
    instrument
        .lock()
        .unwrap()
        .acknowledgements
        .extend(vec![b"\x15\r\n".to_vec(); 3]);
    let error = link.query("AYT", true, &Context::test(1.0)).unwrap_err();
    assert!(
        error.message.contains("AYT [waiting for ACK]")
            && error.message.contains("NAK")
            && error.message.contains("3 transmissions")
    );
    assert_eq!(link.nak_count, 3);
    assert_eq!(writes(&instrument), vec![b"AYT\r".to_vec(); 3]);
}

#[test]
fn etx_is_flushed_settled_and_purged_before_identification() {
    let (mut link, instrument) = link();
    instrument.lock().unwrap().input.extend(b"old input");
    instrument.lock().unwrap().flush_tail = b"\x15\r\n".to_vec();
    link.initialize(&Context::test(1.0)).unwrap();
    assert_eq!(instrument.lock().unwrap().clears, 2);
    assert_eq!(instrument.lock().unwrap().flushes, 1);
    assert_eq!(link.nak_count, 0);
    assert_eq!(writes(&instrument)[..2], [vec![3], b"AYT\r".to_vec()]);
}

#[test]
fn cancellation_before_command_during_etx_or_after_ack_prevents_the_next_write() {
    let ctx = Context::test(1.0);
    ctx.cancel();
    let (mut link, instrument) = link();
    assert!(link.query("PRX", false, &ctx).is_err());
    assert!(writes(&instrument).is_empty());
    let ctx = Context::test(1.0);
    let (mut link, instrument) = self::link();
    instrument.lock().unwrap().cancel_after_etx = Some(ctx.clone());
    assert!(
        link.initialize(&ctx)
            .unwrap_err()
            .message
            .contains("cancelled")
    );
    assert_eq!(writes(&instrument), [vec![3]]);
    for synchronizing in [false, true] {
        let ctx = Context::test(1.0);
        let (mut link, instrument) = self::link();
        instrument.lock().unwrap().cancel_after_ack = Some(ctx.clone());
        assert!(
            link.query("UNI", synchronizing, &ctx)
                .unwrap_err()
                .message
                .contains("cancelled")
        );
        assert_eq!(writes(&instrument), [b"UNI\r".to_vec()]);
    }
}

#[test]
fn protocol_errors_identify_command_and_exact_handshake_phase() {
    for (write, phase) in [
        (b"UNI\r".to_vec(), "sending command"),
        (vec![5], "sending ENQ"),
    ] {
        let (mut link, instrument) = link();
        instrument.lock().unwrap().failed_write = Some(write);
        let error = link.query("UNI", false, &Context::test(1.0)).unwrap_err();
        assert!(
            error.message.contains(&format!("UNI [{phase}]"))
                && error.message.contains("USB transport failed")
        );
    }
    for data_phase in [false, true] {
        let (mut link, instrument) = link();
        if data_phase {
            instrument
                .lock()
                .unwrap()
                .responses
                .insert("UNI".into(), b"4\r".to_vec());
        } else {
            instrument
                .lock()
                .unwrap()
                .acknowledgements
                .push_back(b"\x06\r".to_vec());
        }
        let start = Instant::now();
        let error = link.query("UNI", false, &Context::test(1.0)).unwrap_err();
        let phase = if data_phase {
            "waiting for data"
        } else {
            "waiting for ACK"
        };
        assert!(
            error.message.contains(&format!("UNI [{phase}]"))
                && error.message.contains("timed out")
        );
        assert!(start.elapsed() < Duration::from_millis(200));
    }
}

#[test]
fn short_writes_io_errors_non_ascii_and_reply_length_are_not_successes() {
    for bytes in [b"UNI\r".to_vec(), vec![5]] {
        let (mut link, instrument) = link();
        instrument.lock().unwrap().short_write = Some(bytes);
        assert!(
            link.query("UNI", false, &Context::test(1.0))
                .unwrap_err()
                .message
                .contains("Incomplete serial write")
        );
    }
    let (mut link, instrument) = link();
    instrument.lock().unwrap().read_error = Some(io::ErrorKind::BrokenPipe);
    assert!(
        link.query("UNI", false, &Context::test(1.0))
            .unwrap_err()
            .message
            .contains("waiting for ACK")
    );
    for response in [b"\xff\r\n".to_vec(), vec![b'x'; 513]] {
        let (mut link, instrument) = self::link();
        instrument
            .lock()
            .unwrap()
            .responses
            .insert("UNI".into(), response);
        assert!(link.query("UNI", false, &Context::test(1.0)).is_err());
    }
}

#[test]
fn pressure_unit_change_and_wrong_identity_stop_without_settings_writes() {
    let (mut link, instrument) = link();
    link.initialize(&Context::test(1.0)).unwrap();
    instrument
        .lock()
        .unwrap()
        .responses
        .insert("UNI".into(), b"1\r\n".to_vec());
    assert!(
        link.read_pressures(&Context::test(1.0))
            .unwrap_err()
            .message
            .contains("unit changed")
    );
    let (mut link, instrument) = self::link();
    instrument
        .lock()
        .unwrap()
        .responses
        .insert("AYT".into(), b"TPG362,x,x,x,x\r\n".to_vec());
    assert!(
        link.initialize(&Context::test(1.0))
            .unwrap_err()
            .message
            .contains("TPG366")
    );
    assert!(!writes(&instrument).contains(&b"PRX\r".to_vec()));
    let (mut link, instrument) = self::link();
    instrument
        .lock()
        .unwrap()
        .responses
        .insert("TID".into(), b"TPR,IKR\r\n".to_vec());
    assert!(
        link.initialize(&Context::test(1.0))
            .unwrap_err()
            .message
            .contains("six")
    );
}

#[test]
fn commands_that_can_change_hardware_or_inject_another_mnemonic_are_rejected() {
    let (mut link, instrument) = link();
    for command in [
        "SEN",
        "SEN,2,2,2,2,2,2",
        "UNI,0",
        "DGS",
        "RES",
        "SP1",
        "SAV",
        "COM",
        "PRX\rSEN",
    ] {
        assert_eq!(
            link.query(command, false, &Context::test(1.0))
                .unwrap_err()
                .kind,
            "ValueError"
        );
    }
    assert!(writes(&instrument).is_empty());
}

#[test]
fn constructor_validation_and_rpc_inventory_are_strict_without_opening_serial() {
    for (key, value) in [
        ("port", json!("")),
        ("port", json!("COM1\0")),
        ("baudrate", json!(true)),
        ("baudrate", json!(123)),
        ("timeout_s", json!(false)),
        ("timeout_s", json!(0)),
        ("timeout_s", json!(31)),
        ("etx_settle_s", json!("1")),
        ("etx_settle_s", json!(-1)),
    ] {
        let mut config = json!({"port":"never-opened"});
        config[key] = value;
        assert!(Controller::new(&config).is_err(), "{key}");
    }
    let mut controller = Controller::new(&json!({"port":"never-opened"})).unwrap();
    for method in GUI_METHODS {
        assert_eq!(
            controller
                .call(method, &[], &json!({"unexpected":1}), &Context::test(1.0))
                .unwrap_err()
                .kind,
            "TypeError"
        );
    }
    for attr in GUI_ATTRIBUTES {
        assert!(controller.get_attribute(attr).is_ok(), "{attr}");
    }
    assert!(
        controller
            .call(
                "read_pressures",
                &[json!(1)],
                &json!({}),
                &Context::test(1.0)
            )
            .is_err()
    );
    assert!(
        controller
            .call("read_pressures", &[], &json!({}), &Context::test(1.0))
            .is_err()
    );
    assert_eq!(
        controller
            .call("disconnect", &[], &json!({}), &Context::test(1.0))
            .unwrap(),
        json!(true)
    );
}
