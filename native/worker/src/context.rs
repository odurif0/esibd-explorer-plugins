use crate::error::{Error, Result};
use serde_json::Value;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
    mpsc::{SyncSender, TrySendError},
};
use std::time::{Duration, Instant};

struct WatchedCall {
    symbol: String,
    sequence: u64,
    deadline: Instant,
    timeout: Duration,
}

pub struct NativeWatchdog {
    active: Mutex<Option<WatchedCall>>,
}

impl NativeWatchdog {
    // Idle housekeeping has no active parent RPC to supervise a blocked DLL call.
    pub fn spawn() -> Arc<Self> {
        let watchdog = Arc::new(Self {
            active: Mutex::new(None),
        });
        let observer = watchdog.clone();
        std::thread::spawn(move || {
            loop {
                {
                    let active = observer.active.lock().unwrap();
                    if let Some(call) = active
                        .as_ref()
                        .filter(|call| Instant::now() >= call.deadline)
                    {
                        crate::error::exit_with_diagnostic(
                            77,
                            format!(
                                "{} blocked past its native I/O deadline ({:.3}s) during housekeeping; transport is unusable and output safety is not confirmed",
                                call.symbol,
                                call.timeout.as_secs_f64()
                            ),
                        );
                    }
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        });
        watchdog
    }

    fn enter(
        self: &Arc<Self>,
        symbol: &str,
        sequence: u64,
        timeout: Duration,
    ) -> Result<WatchdogGuard> {
        let mut active = self.active.lock().unwrap();
        if active.is_some() {
            return Err(Error::runtime(
                "Overlapping housekeeping DLL calls are forbidden",
            ));
        }
        *active = Some(WatchedCall {
            symbol: symbol.into(),
            sequence,
            deadline: Instant::now() + timeout,
            timeout,
        });
        Ok(WatchdogGuard {
            watchdog: self.clone(),
            sequence,
        })
    }
}

struct WatchdogGuard {
    watchdog: Arc<NativeWatchdog>,
    sequence: u64,
}

impl Drop for WatchdogGuard {
    fn drop(&mut self) {
        let mut active = self.watchdog.active.lock().unwrap();
        if active
            .as_ref()
            .is_some_and(|call| call.sequence == self.sequence)
        {
            *active = None;
        }
    }
}

#[derive(Clone)]
pub struct Context {
    deadline: Instant,
    cancel: Arc<AtomicBool>,
    progress: Option<SyncSender<Value>>,
    request_id: Option<u64>,
    native_timeout: Option<Duration>,
    native_sequence: Arc<AtomicU64>,
    watchdog: Option<Arc<NativeWatchdog>>,
}

impl Context {
    pub fn new(
        timeout: Duration,
        cancel: Arc<AtomicBool>,
        progress: Option<SyncSender<Value>>,
    ) -> Self {
        Self {
            deadline: Instant::now() + timeout,
            cancel,
            progress,
            request_id: None,
            native_timeout: None,
            native_sequence: Arc::new(AtomicU64::new(0)),
            watchdog: None,
        }
    }

    pub fn with_request_id(mut self, id: u64) -> Self {
        self.request_id = Some(id);
        self
    }
    pub fn with_native_timeout(mut self, timeout: Duration) -> Self {
        self.native_timeout = Some(timeout);
        self
    }
    pub fn with_watchdog(mut self, watchdog: Arc<NativeWatchdog>) -> Self {
        self.watchdog = Some(watchdog);
        self
    }

    pub fn cleanup(&self, timeout: Duration) -> Self {
        Self {
            deadline: Instant::now() + timeout,
            cancel: Arc::new(AtomicBool::new(false)),
            progress: self.progress.clone(),
            request_id: self.request_id,
            native_timeout: self.native_timeout,
            native_sequence: self.native_sequence.clone(),
            watchdog: self.watchdog.clone(),
        }
    }

    fn emit(&self, message: Value) -> Result<()> {
        if let Some(sender) = &self.progress {
            let until = Instant::now() + Duration::from_millis(50).min(self.remaining());
            let mut message = message;
            loop {
                match sender.try_send(message) {
                    Ok(()) => break,
                    Err(TrySendError::Full(value)) if Instant::now() < until => {
                        message = value;
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    Err(error) => {
                        return Err(Error::new(
                            "TimeoutError",
                            format!("Native supervision transport is unusable: {error}"),
                        ));
                    }
                }
            }
        }
        Ok(())
    }

    pub fn native_call<T>(
        &self,
        symbol: &str,
        timeout: Option<Duration>,
        call: impl FnOnce() -> Result<T>,
    ) -> Result<T> {
        let limit = timeout
            .or(self.native_timeout)
            .unwrap_or(self.remaining())
            .min(self.remaining());
        if limit.is_zero() {
            return Err(Error::new(
                "TimeoutError",
                "Native I/O deadline expired before dispatch",
            ));
        }
        let sequence = self.native_sequence.fetch_add(1, Ordering::Relaxed);
        let started = Instant::now();
        if let Some(id) = self.request_id {
            self.emit(
                serde_json::json!({"version":1,"id":id,"kind":"native_io","phase":"enter",
                "sequence":sequence,"symbol":symbol,"timeout_s":limit.as_secs_f64()}),
            )?;
        }
        let guard = self
            .watchdog
            .as_ref()
            .map(|watchdog| watchdog.enter(symbol, sequence, limit))
            .transpose()?;
        let result = call();
        drop(guard);
        let elapsed = started.elapsed();
        if let Some(id) = self.request_id {
            self.emit(
                serde_json::json!({"version":1,"id":id,"kind":"native_io","phase":"exit",
                "sequence":sequence,"symbol":symbol,"elapsed_s":elapsed.as_secs_f64()}),
            )?;
        }
        if elapsed >= limit {
            return Err(Error::new(
                "TimeoutError",
                format!(
                    "{symbol} exceeded its native I/O deadline ({:.3}s); transport is unusable",
                    limit.as_secs_f64()
                ),
            ));
        }
        result
    }

    pub fn test(timeout_seconds: f64) -> Self {
        Self::new(
            Duration::from_secs_f64(timeout_seconds),
            Arc::new(AtomicBool::new(false)),
            None,
        )
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancel.load(Ordering::Acquire)
    }
    pub fn cancel(&self) {
        self.cancel.store(true, Ordering::Release);
    }
    pub fn remaining(&self) -> Duration {
        self.deadline.saturating_duration_since(Instant::now())
    }

    pub fn check_cancelled(&self) -> Result<()> {
        if self.is_cancelled() {
            return Err(Error::runtime("Operation cancelled"));
        }
        if self.remaining().is_zero() {
            return Err(Error::new("TimeoutError", "Operation deadline expired"));
        }
        Ok(())
    }

    pub fn sleep(&self, duration: Duration) -> Result<()> {
        let until = Instant::now() + duration;
        while Instant::now() < until {
            self.check_cancelled()?;
            std::thread::sleep(
                Duration::from_millis(20)
                    .min(until.saturating_duration_since(Instant::now()))
                    .min(self.remaining()),
            );
        }
        self.check_cancelled()
    }

    pub fn progress(&self, value: Value) -> Result<()> {
        self.check_cancelled()?;
        if self.progress.is_some() {
            let value = match self.request_id {
                Some(id) => {
                    serde_json::json!({"version":1,"id":id,"kind":"progress","value":value})
                }
                None => value,
            };
            self.emit(value)?;
        }
        Ok(())
    }
}
