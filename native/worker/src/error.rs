use serde::Serialize;

pub type Result<T> = std::result::Result<T, Error>;

fn write_diagnostic(message: &str) {
    use std::io::Write;
    let _ = writeln!(std::io::stderr().lock(), "{message}");
}

pub fn diagnostic(message: impl Into<String>) {
    static LOGGER: std::sync::OnceLock<Option<std::sync::mpsc::SyncSender<String>>> =
        std::sync::OnceLock::new();
    let logger = LOGGER.get_or_init(|| {
        let (sender, messages) = std::sync::mpsc::sync_channel::<String>(32);
        std::thread::Builder::new()
            .name("native-diagnostics".into())
            .spawn(move || {
                for message in messages {
                    write_diagnostic(&message);
                }
            })
            .ok()
            .map(|_| sender)
    });
    if let Some(sender) = logger {
        let _ = sender.try_send(message.into());
    }
}

// Fatal exit must not wait for a full stderr pipe or a blocked diagnostic sink.
pub fn exit_with_diagnostic(code: i32, message: impl Into<String>) -> ! {
    let message = message.into();
    let (done, completion) = std::sync::mpsc::sync_channel(1);
    if std::thread::Builder::new()
        .name("native-fatal-log".into())
        .spawn(move || {
            write_diagnostic(&message);
            let _ = done.try_send(());
        })
        .is_ok()
    {
        let _ = completion.recv_timeout(std::time::Duration::from_millis(10));
    }
    std::process::exit(code)
}

#[derive(Debug, Clone, Serialize)]
pub struct Error {
    pub kind: String,
    pub message: String,
}

impl Error {
    pub fn new(kind: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            kind: kind.into(),
            message: message.into(),
        }
    }

    pub fn argument(message: impl Into<String>) -> Self {
        Self::new("ValueError", message)
    }
    pub fn runtime(message: impl Into<String>) -> Self {
        Self::new("RuntimeError", message)
    }
    pub fn unsupported(message: impl Into<String>) -> Self {
        Self::new("NotImplementedError", message)
    }
    pub fn status(code: i64, operation: &str) -> Self {
        Self::runtime(format!("{operation} failed with vendor status {code}"))
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.kind, self.message)
    }
}

impl std::error::Error for Error {}
