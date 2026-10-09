// SPDX-License-Identifier: GPL-2.0-or-later
use crate::{
    Backend,
    codec::{float, tuple},
    context::Context,
    error::{Error, Result},
};
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub trait Port: Read + Write + Send {
    fn clear_input(&mut self) -> std::io::Result<()>;
}

struct Serial {
    port: Box<dyn serialport::SerialPort>,
    #[cfg(unix)]
    _lock: std::fs::File,
}

impl Serial {
    fn open(name: &str, baudrate: u32) -> Result<Self> {
        let builder = serialport::new(name, baudrate)
            .data_bits(serialport::DataBits::Eight)
            .parity(serialport::Parity::None)
            .stop_bits(serialport::StopBits::One)
            .flow_control(serialport::FlowControl::None)
            .timeout(Duration::from_millis(50));
        #[cfg(unix)]
        {
            use std::os::fd::{AsRawFd, BorrowedFd};
            // TIOCEXCL can survive a killed PTY worker. flock is released on process exit.
            let port = builder
                .exclusive(false)
                .open_native()
                .map_err(|e| Error::new("OSError", format!("opening USB port: {e}")))?;
            // The live port owns this fd while the borrowed handle is cloned.
            let fd = unsafe { BorrowedFd::borrow_raw(port.as_raw_fd()) }
                .try_clone_to_owned()
                .map_err(|e| Error::new("OSError", format!("opening USB port: {e}")))?;
            let lock = std::fs::File::from(fd);
            lock.try_lock().map_err(|e| {
                Error::new(
                    "OSError",
                    format!("opening USB port: exclusive lock failed: {e}"),
                )
            })?;
            Ok(Self {
                port: Box::new(port),
                _lock: lock,
            })
        }
        #[cfg(not(unix))]
        {
            let port = builder
                .open()
                .map_err(|e| Error::new("OSError", format!("opening USB port: {e}")))?;
            Ok(Self { port })
        }
    }
}

impl Read for Serial {
    fn read(&mut self, data: &mut [u8]) -> std::io::Result<usize> {
        self.port.read(data)
    }
}
impl Write for Serial {
    fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
        self.port.write(data)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.port.flush()
    }
}
impl Port for Serial {
    fn clear_input(&mut self) -> std::io::Result<()> {
        self.port
            .clear(serialport::ClearBuffer::Input)
            .map_err(std::io::Error::other)
    }
}

fn protocol(message: impl Into<String>) -> Error {
    Error::new("ProtocolError", message)
}

pub const GUI_METHODS: &[&str] = &["initialize", "read_pressures", "close", "disconnect"];
pub const GUI_ATTRIBUTES: &[&str] = &["connected", "nak_count", "unit", "identification", "gauges"];

pub fn parse_pressures(reply: &str, unit: u8, received_at: f64) -> Result<Value> {
    let (label, factor) = match unit {
        0 => ("mbar", 1.),
        1 => ("Torr", 1013.25 / 760.),
        2 => ("Pa", 0.01),
        3 => ("Micron", 1013.25 / 760_000.),
        4 => ("hPa", 1.),
        _ => return Err(protocol("TPG366 reports a non-pressure unit")),
    };
    let fields: Vec<&str> = reply.split(',').map(str::trim).collect();
    if fields.len() != 12 {
        return Err(protocol("Expected six status/pressure pairs"));
    }
    let mut pressures = Vec::with_capacity(6);
    let mut statuses = Vec::with_capacity(6);
    for pair in fields.as_chunks::<2>().0 {
        if pair[0].len() != 1 || !matches!(pair[0].as_bytes()[0], b'0'..=b'6') {
            return Err(protocol("Unknown gauge status"));
        }
        let status = pair[0].as_bytes()[0] - b'0';
        let value = pair[1]
            .parse::<f64>()
            .map_err(|_| protocol("Invalid pressure number"))?
            * factor;
        if !value.is_finite() {
            return Err(protocol("Non-finite pressure"));
        }
        pressures.push(float(if status == 0 { value } else { f64::NAN }));
        statuses.push(json!(status));
    }
    if !received_at.is_finite() {
        return Err(protocol("Invalid sample timestamp"));
    }
    Ok(
        json!({"pressures":tuple(pressures),"statuses":tuple(statuses),"unit":label,"received_at":received_at}),
    )
}

pub struct Link {
    port: Box<dyn Port>,
    timeout: Duration,
    etx_settle: Duration,
    pub identification: String,
    pub gauges: Vec<String>,
    pub unit: Option<u8>,
    pub nak_count: u64,
}

impl Link {
    pub fn new(port: Box<dyn Port>, timeout: Duration, etx_settle: Duration) -> Self {
        Self {
            port,
            timeout,
            etx_settle,
            identification: String::new(),
            gauges: Vec::new(),
            unit: None,
            nak_count: 0,
        }
    }
    fn write(&mut self, data: &[u8], ctx: &Context) -> Result<()> {
        ctx.check_cancelled()?;
        match self.port.write(data) {
            Ok(count) if count == data.len() => Ok(()),
            Ok(_) => Err(protocol("Incomplete serial write")),
            Err(error) => Err(protocol(format!("Serial write: {error}"))),
        }
    }
    fn line(&mut self, deadline: Instant, ctx: &Context) -> Result<Vec<u8>> {
        let mut bytes = Vec::new();
        while Instant::now() < deadline {
            ctx.check_cancelled()?;
            let mut byte = [0u8; 1];
            match self.port.read(&mut byte) {
                Ok(1) => {
                    bytes.push(byte[0]);
                    if bytes.ends_with(b"\r\n") {
                        bytes.truncate(bytes.len() - 2);
                        return Ok(bytes);
                    }
                    if bytes.len() > 512 {
                        return Err(protocol("Serial reply exceeded 512 bytes"));
                    }
                }
                Ok(_) => {}
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::TimedOut
                            | std::io::ErrorKind::WouldBlock
                            | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(protocol(format!("Serial read: {e}"))),
            }
        }
        ctx.check_cancelled()?;
        Err(protocol(format!(
            "Serial reply timed out; partial={bytes:?}"
        )))
    }
    pub fn query(&mut self, command: &str, synchronizing: bool, ctx: &Context) -> Result<String> {
        if !matches!(command, "AYT" | "TID" | "UNI" | "PRX") {
            return Err(Error::argument("Unsupported read-only TPG366 command"));
        }
        for attempt in 1..=3 {
            match self.exchange(command, synchronizing, ctx) {
                Err(error) if error.kind == "Rejected" => {
                    self.nak_count = self.nak_count.saturating_add(1);
                    if attempt == 3 {
                        return Err(protocol(format!(
                            "{command} [waiting for ACK]: TPG366 returned NAK (received {}) to 3 transmissions",
                            error.message
                        )));
                    }
                }
                outcome => return outcome,
            }
        }
        Err(protocol("TPG366 exhausted retransmissions"))
    }
    fn exchange(&mut self, command: &str, synchronizing: bool, ctx: &Context) -> Result<String> {
        let deadline = Instant::now() + self.timeout.min(ctx.remaining());
        let mut phase = "sending command";
        let outcome = (|| {
            self.write(format!("{command}\r").as_bytes(), ctx)?;
            phase = "waiting for ACK";
            let mut ack = self.line(deadline, ctx)?;
            loop {
                let prefix = ack
                    .iter()
                    .take_while(|byte| **byte == b'\r' || **byte == b'\n')
                    .count();
                let control = if synchronizing { &ack[prefix..] } else { &ack };
                if control == [0x15] {
                    return Err(Error::new("Rejected", format!("{ack:?}")));
                }
                if control == [0x06] {
                    phase = "sending ENQ";
                    self.write(&[0x05], ctx)?;
                    phase = "waiting for data";
                    let bytes = self.line(deadline, ctx)?;
                    if !bytes.is_ascii() {
                        return Err(protocol("Non-ASCII serial reply"));
                    }
                    return String::from_utf8(bytes).map_err(|_| protocol("Invalid serial text"));
                }
                if !synchronizing || ack.contains(&0x06) || ack.contains(&0x15) {
                    return Err(protocol(format!("Malformed acknowledgement: {ack:?}")));
                }
                ack = self.line(deadline, ctx)?;
            }
        })();
        outcome.map_err(|error| {
            if error.kind == "ProtocolError" {
                protocol(format!("{command} [{phase}]: {}", error.message))
            } else {
                error
            }
        })
    }
    fn read_unit(&mut self, ctx: &Context) -> Result<u8> {
        let reply = self.query("UNI", false, ctx)?;
        let trimmed = reply.trim();
        if trimmed.len() != 1 || !matches!(trimmed.as_bytes()[0], b'0'..=b'4') {
            return Err(protocol(format!("Unsupported pressure unit UNI={reply}")));
        }
        Ok(trimmed.as_bytes()[0] - b'0')
    }
    pub fn initialize(&mut self, ctx: &Context) -> Result<()> {
        ctx.check_cancelled()?;
        self.port
            .clear_input()
            .map_err(|e| protocol(e.to_string()))?;
        self.write(&[0x03], ctx)?;
        self.port.flush().map_err(|e| protocol(e.to_string()))?;
        ctx.sleep(self.etx_settle)?;
        self.port
            .clear_input()
            .map_err(|e| protocol(e.to_string()))?;
        self.identification = self.query("AYT", true, ctx)?;
        let parts: Vec<&str> = self.identification.split(',').collect();
        if parts.len() != 5 || parts[0].trim().replace(' ', "").to_uppercase() != "TPG366" {
            return Err(protocol("Port did not identify a TPG366"));
        }
        self.gauges = self
            .query("TID", false, ctx)?
            .split(',')
            .map(|v| v.trim().to_owned())
            .collect();
        if self.gauges.len() != 6 {
            return Err(protocol("Expected six gauge identifications"));
        }
        self.unit = Some(self.read_unit(ctx)?);
        Ok(())
    }
    pub fn read_pressures(&mut self, ctx: &Context) -> Result<Value> {
        if self.unit.is_none() {
            self.unit = Some(self.read_unit(ctx)?);
        }
        let reply = self.query("PRX", false, ctx)?;
        let received_at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| protocol("Invalid system time"))?
            .as_secs_f64();
        let unit = self.read_unit(ctx)?;
        if self.unit != Some(unit) {
            return Err(protocol(
                "Pressure unit changed during read; reconnect to continue",
            ));
        }
        ctx.check_cancelled()?;
        parse_pressures(&reply, unit, received_at)
    }
}

pub struct Controller {
    port_name: String,
    baudrate: u32,
    timeout: Duration,
    etx_settle: Duration,
    link: Option<Link>,
}

impl Controller {
    pub fn new(config: &Value) -> Result<Self> {
        let port_name = config
            .get("port")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty() && !s.contains('\0'))
            .ok_or_else(|| Error::argument("Missing serial port"))?
            .to_owned();
        let baudrate = match config.get("baudrate") {
            Some(value) => value
                .as_u64()
                .ok_or_else(|| Error::new("TypeError", "baudrate must be an integer"))?,
            None => 9600,
        };
        if ![9600, 19200, 38400, 57600, 115200].contains(&baudrate) {
            return Err(Error::argument("Unsupported TPG366 baudrate"));
        }
        let timeout = timing(config.get("timeout_s"), 1., "timeout_s")?;
        let etx_settle = timing(config.get("etx_settle_s"), 0.2, "etx_settle_s")?;
        if !timeout.is_finite()
            || !(0.05..=30.).contains(&timeout)
            || !etx_settle.is_finite()
            || !(0.0..=5.).contains(&etx_settle)
        {
            return Err(Error::argument("Invalid serial timing"));
        }
        Ok(Self {
            port_name,
            baudrate: baudrate as u32,
            timeout: Duration::from_secs_f64(timeout),
            etx_settle: Duration::from_secs_f64(etx_settle),
            link: None,
        })
    }

    fn initialize(&mut self, ctx: &Context) -> Result<Value> {
        if self.link.is_none() {
            self.link = Some(Link::new(
                Box::new(Serial::open(&self.port_name, self.baudrate)?),
                self.timeout,
                self.etx_settle,
            ));
        }
        // Resynchronization owns the same open handle, including after a failed handshake.
        let link = self
            .link
            .as_mut()
            .ok_or_else(|| Error::runtime("TPG366 is disconnected"))?;
        link.initialize(ctx)?;
        Ok(
            json!({"identification":link.identification,"gauges":tuple(link.gauges.iter().map(|g| json!(g)).collect()),"unit":link.unit}),
        )
    }
}

fn timing(value: Option<&Value>, default: f64, name: &str) -> Result<f64> {
    value
        .map(|value| {
            value
                .as_f64()
                .ok_or_else(|| Error::new("TypeError", format!("{name} must be numeric")))
        })
        .unwrap_or(Ok(default))
}

impl Backend for Controller {
    fn call(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        ctx.check_cancelled()?;
        if !kwargs.is_null() && !kwargs.is_object() {
            return Err(Error::new("TypeError", "kwargs must be an object"));
        }
        if kwargs.as_object().is_some_and(|values| !values.is_empty()) {
            return Err(Error::new(
                "TypeError",
                "TPG366 methods take no keyword arguments",
            ));
        }
        if args.len() != usize::from(method == "query") {
            return Err(Error::new(
                "TypeError",
                "Incorrect TPG366 positional arguments",
            ));
        }
        let outcome = (|| match method {
            "initialize" => self.initialize(ctx),
            "read_pressures" => self
                .link
                .as_mut()
                .ok_or_else(|| Error::runtime("TPG366 is disconnected"))?
                .read_pressures(ctx),
            "query" => {
                let command = args
                    .first()
                    .and_then(Value::as_str)
                    .ok_or_else(|| Error::argument("Missing read command"))?;
                Ok(json!(
                    self.link
                        .as_mut()
                        .ok_or_else(|| Error::runtime("TPG366 is disconnected"))?
                        .query(command, false, ctx)?
                ))
            }
            "close" | "disconnect" => {
                self.link = None;
                Ok(json!(true))
            }
            _ => Err(Error::unsupported(format!(
                "Unknown TPG366 method: {method}"
            ))),
        })();
        // Progress is consumed by the local adapter even when the transaction failed.
        let _ = ctx.progress(json!({"family":"tpg366","nak_count":self.link.as_ref().map_or(0, |link| link.nak_count)}));
        outcome
    }
    fn get_attribute(&self, name: &str) -> Result<Value> {
        match name {
            "connected" => Ok(json!(self.link.is_some())),
            "nak_count" => Ok(json!(self.link.as_ref().map_or(0, |l| l.nak_count))),
            "unit" => Ok(json!(self.link.as_ref().and_then(|l| l.unit))),
            "identification" => Ok(json!(
                self.link
                    .as_ref()
                    .map(|l| l.identification.as_str())
                    .unwrap_or("")
            )),
            "gauges" => Ok(tuple(
                self.link
                    .as_ref()
                    .map(|l| l.gauges.iter().map(|g| json!(g)).collect())
                    .unwrap_or_default(),
            )),
            _ => Err(Error::new("AttributeError", name)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        collections::VecDeque,
        sync::{Arc, Mutex},
    };
    struct Fake {
        input: VecDeque<u8>,
        writes: Arc<Mutex<Vec<Vec<u8>>>>,
    }
    impl Read for Fake {
        fn read(&mut self, data: &mut [u8]) -> std::io::Result<usize> {
            match self.input.pop_front() {
                Some(byte) => {
                    data[0] = byte;
                    Ok(1)
                }
                None => Err(std::io::ErrorKind::TimedOut.into()),
            }
        }
    }
    impl Write for Fake {
        fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
            self.writes.lock().unwrap().push(data.to_vec());
            Ok(data.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    impl Port for Fake {
        fn clear_input(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    fn link(input: &[u8]) -> (Link, Arc<Mutex<Vec<Vec<u8>>>>) {
        let writes = Arc::new(Mutex::new(Vec::new()));
        (
            Link::new(
                Box::new(Fake {
                    input: input.iter().copied().collect(),
                    writes: writes.clone(),
                }),
                Duration::from_millis(50),
                Duration::ZERO,
            ),
            writes,
        )
    }
    #[test]
    fn sentinels_units_and_negative_offsets() {
        let value = parse_pressures("0,-2,1,1,2,2,3,3,4,4,6,6", 2, 42.).unwrap();
        assert_eq!(value["pressures"]["$tuple"][0], json!(-0.02));
        assert_eq!(value["pressures"]["$tuple"][1], json!({"$float":"nan"}));
        assert!(parse_pressures("0,NaN,0,1,0,1,0,1,0,1,0,1", 0, 0.).is_err());
        assert!(parse_pressures("0,1", 0, 0.).is_err());
    }
    #[test]
    fn nak_retransmission_is_bounded_and_read_only() {
        let (mut link, writes) = link(b"\x15\r\n\x15\r\n\x06\r\n0\r\n");
        assert_eq!(link.query("UNI", false, &Context::test(1.)).unwrap(), "0");
        assert_eq!(link.nak_count, 2);
        assert_eq!(
            *writes.lock().unwrap(),
            vec![
                b"UNI\r".to_vec(),
                b"UNI\r".to_vec(),
                b"UNI\r".to_vec(),
                vec![5]
            ]
        );
        assert!(link.query("UNI,0", false, &Context::test(1.)).is_err());
    }
    #[test]
    fn unit_change_discards_frame() {
        let (mut link, _) = link(b"\x06\r\n0,1,0,1,0,1,0,1,0,1,0,1\r\n\x06\r\n2\r\n");
        link.unit = Some(0);
        assert!(
            link.read_pressures(&Context::test(1.))
                .unwrap_err()
                .message
                .contains("unit changed")
        );
    }
    #[test]
    fn cancellation_prevents_writes() {
        let (mut link, writes) = link(b"");
        let ctx = Context::test(1.);
        ctx.cancel();
        assert!(link.query("PRX", false, &ctx).is_err());
        assert!(writes.lock().unwrap().is_empty());
    }
}
