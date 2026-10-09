use esibd_native_worker::{
    Backend,
    context::{Context, NativeWatchdog},
    error::{Error, Result, exit_with_diagnostic},
    factory, protocol,
};
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
    mpsc::{self, SyncSender},
};
use std::time::Duration;
use std::{collections::HashMap, fs::File};

type Active = Arc<Mutex<HashMap<u64, Arc<AtomicBool>>>>;

fn protocol_output() -> std::io::Result<File> {
    #[cfg(unix)]
    {
        use std::os::fd::FromRawFd;
        let descriptor = unsafe { libc::dup(libc::STDOUT_FILENO) };
        if descriptor < 0 {
            return Err(std::io::Error::last_os_error());
        }
        if unsafe { libc::dup2(libc::STDERR_FILENO, libc::STDOUT_FILENO) } < 0 {
            unsafe {
                libc::close(descriptor);
            }
            return Err(std::io::Error::last_os_error());
        }
        Ok(unsafe { File::from_raw_fd(descriptor) })
    }
    #[cfg(windows)]
    {
        use std::os::windows::io::FromRawHandle;
        use windows_sys::Win32::{
            Foundation::{DUPLICATE_SAME_ACCESS, DuplicateHandle},
            System::{
                Console::{GetStdHandle, STD_ERROR_HANDLE, STD_OUTPUT_HANDLE, SetStdHandle},
                Threading::GetCurrentProcess,
            },
        };
        #[link(name = "msvcrt")]
        unsafe extern "C" {
            fn _dup2(source: i32, target: i32) -> i32;
        }
        let process = unsafe { GetCurrentProcess() };
        let mut duplicate = 0;
        if unsafe {
            DuplicateHandle(
                process,
                GetStdHandle(STD_OUTPUT_HANDLE),
                process,
                &mut duplicate,
                0,
                0,
                DUPLICATE_SAME_ACCESS,
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error());
        }
        let file = unsafe { File::from_raw_handle(duplicate as *mut _) };
        if unsafe { SetStdHandle(STD_OUTPUT_HANDLE, GetStdHandle(STD_ERROR_HANDLE)) } == 0
            || unsafe { _dup2(2, 1) } != 0
        {
            return Err(std::io::Error::last_os_error());
        }
        Ok(file)
    }
}

fn send(sender: &SyncSender<Value>, value: Value) {
    if sender.try_send(value).is_err() {
        exit_with_diagnostic(75, "Worker output queue exhausted or closed");
    }
}

fn lifecycle(backend: &Option<Box<dyn Backend>>) -> Value {
    let mut state = serde_json::Map::new();
    if let Some(controller) = backend {
        // These getters are cached controller state, never additional DLL calls.
        for name in [
            "connected",
            "_dll_port_claimed",
            "_open_failed",
            "_opening_in_progress",
            "_failed_open_released",
            "_failed_open_cleanup_outcome",
            "_transport_poisoned",
            "_transport_error",
        ] {
            if let Ok(value) = controller.get_attribute(name) {
                state.insert(name.into(), value);
            }
        }
    }
    Value::Object(state)
}

fn execute(
    backend: &mut Option<Box<dyn Backend>>,
    request: &Value,
    family: &str,
    ctx: &Context,
) -> Result<Value> {
    let op = request
        .get("op")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::argument("Missing operation"))?;
    if op == "init" {
        if backend.is_some()
            || request.get("id").and_then(Value::as_u64) != Some(0)
            || request.get("family").and_then(Value::as_str) != Some(family)
        {
            return Err(Error::argument("Invalid or repeated initialization/family"));
        }
        *backend = Some(factory::create(
            family,
            request.get("config").unwrap_or(&json!({})),
        )?);
        return Ok(
            json!({"family":family,"protocol":protocol::VERSION,"worker_version":env!("CARGO_PKG_VERSION")}),
        );
    }
    let controller = backend
        .as_mut()
        .ok_or_else(|| Error::runtime("Worker is not initialized"))?;
    ctx.check_cancelled()?;
    match op {
        "call" => {
            let method = request
                .get("method")
                .and_then(Value::as_str)
                .ok_or_else(|| Error::argument("Missing method"))?;
            let args = request
                .get("args")
                .and_then(Value::as_array)
                .ok_or_else(|| Error::argument("Expected argument list"))?;
            let kwargs = request
                .get("kwargs")
                .filter(|v| v.is_object())
                .ok_or_else(|| Error::argument("Expected keyword object"))?;
            controller.call(method, args, kwargs, ctx)
        }
        "getattr" => controller.get_attribute(
            request
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(|| Error::argument("Missing attribute name"))?,
        ),
        "setattr" => {
            controller.set_attribute(
                request
                    .get("name")
                    .and_then(Value::as_str)
                    .ok_or_else(|| Error::argument("Missing attribute name"))?,
                request.get("value").cloned().unwrap_or(Value::Null),
            )?;
            Ok(Value::Null)
        }
        "close" => {
            *backend = None;
            Ok(Value::Bool(true))
        }
        _ => Err(Error::argument(format!("Unknown operation {op}"))),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 || args[1] != "--family" {
        exit_with_diagnostic(64, "Expected --family <name>");
    }
    let family = args[2].clone();
    let mut output = protocol_output().unwrap_or_else(|e| {
        exit_with_diagnostic(74, format!("Cannot create isolated protocol output: {e}"))
    });
    let (outputs, output_rx) = mpsc::sync_channel::<Value>(64);
    std::thread::spawn(move || {
        for message in output_rx {
            if protocol::write_frame(&mut output, &message).is_err() {
                std::process::exit(74);
            }
        }
    });
    let (requests, request_rx) = mpsc::sync_channel::<(Value, Arc<AtomicBool>)>(8);
    let active: Active = Arc::new(Mutex::new(HashMap::new()));
    let executor_active = active.clone();
    let executor_output = outputs.clone();
    let housekeeping_watchdog = NativeWatchdog::spawn();
    std::thread::spawn(move || {
        let mut backend: Option<Box<dyn Backend>> = None;
        loop {
            let (request, cancellation) = match request_rx.recv_timeout(Duration::from_millis(100))
            {
                Ok(request) => request,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if let Some(controller) = &mut backend {
                        let context = Context::test(5.)
                            .with_native_timeout(Duration::from_secs(5))
                            .with_watchdog(housekeeping_watchdog.clone());
                        if let Err(error) = controller.tick(&context) {
                            esibd_native_worker::error::diagnostic(format!(
                                "Housekeeping: {error}"
                            ));
                        }
                        if controller.get_attribute("_transport_poisoned").ok()
                            == Some(Value::Bool(true))
                        {
                            exit_with_diagnostic(
                                76,
                                "Housekeeping retired an unusable transport; output safety is not confirmed",
                            );
                        }
                    }
                    continue;
                }
                Err(_) => std::process::exit(0),
            };
            let id = request["id"].as_u64().unwrap();
            let timeout = request
                .get("timeout_s")
                .and_then(Value::as_f64)
                .unwrap_or(30.);
            let io_timeout = request
                .get("io_timeout_s")
                .and_then(Value::as_f64)
                .unwrap_or(5.);
            let result = if !timeout.is_finite()
                || !(0.001..=3600.).contains(&timeout)
                || !io_timeout.is_finite()
                || !(0.001..=3600.).contains(&io_timeout)
            {
                Err(Error::argument(
                    "Operation timeout must be between 1ms and 1h",
                ))
            } else {
                let ctx = Context::new(
                    Duration::from_secs_f64(timeout),
                    cancellation,
                    Some(executor_output.clone()),
                )
                .with_request_id(id)
                .with_native_timeout(Duration::from_secs_f64(io_timeout));
                execute(&mut backend, &request, &family, &ctx)
            };
            executor_active.lock().unwrap().remove(&id);
            let mut reply = protocol::reply(id, result);
            reply["state"] = lifecycle(&backend);
            send(&executor_output, reply);
        }
    });
    let mut input = std::io::stdin().lock();
    let mut last_id = None;
    loop {
        let request = match protocol::read_frame(&mut input) {
            Ok(value) => value,
            Err(error) => {
                exit_with_diagnostic(0, format!("Parent pipe closed or invalid: {error}"))
            }
        };
        let id = request["id"].as_u64().unwrap();
        if last_id.is_some_and(|last| id <= last) {
            exit_with_diagnostic(65, "Request ids must increase");
        }
        last_id = Some(id);
        if request.get("op").and_then(Value::as_str) == Some("cancel") {
            let target = request.get("target").and_then(Value::as_u64);
            let mut matched = false;
            if let Some(flag) = target.and_then(|id| active.lock().unwrap().get(&id).cloned()) {
                flag.store(true, Ordering::Release);
                matched = true;
            }
            send(&outputs, protocol::reply(id, Ok(Value::Bool(matched))));
        } else {
            let cancellation = Arc::new(AtomicBool::new(false));
            active.lock().unwrap().insert(id, cancellation.clone());
            if let Err(error) = requests.try_send((request, cancellation)) {
                active.lock().unwrap().remove(&id);
                send(
                    &outputs,
                    protocol::reply(
                        id,
                        Err(Error::runtime(format!(
                            "Worker request queue full or closed: {error}"
                        ))),
                    ),
                );
            }
        }
    }
}
