#[cfg(feature = "test-backend")]
use crate::context::Context;
use crate::{
    Backend,
    error::{Error, Result},
};
use serde_json::Value;

fn vendor(config: &Value, family: &str) -> Result<(Value, Box<dyn crate::ffi::Dll>)> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let path = config
        .get("dll_path")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::argument("Missing vendor DLL path"))?;
    let path = std::path::Path::new(path);
    let kind = std::fs::symlink_metadata(path)
        .map_err(|e| Error::new("OSError", e.to_string()))?
        .file_type();
    if !path.is_absolute() || !kind.is_file() || kind.is_symlink() {
        return Err(Error::argument(
            "Vendor DLL must be an absolute regular file, not a symlink",
        ));
    }
    if !cfg!(windows) {
        return Err(Error::new(
            "OSError",
            "CGC devices require a Windows x86-64 native worker",
        ));
    }
    let mut input = std::fs::File::open(path).map_err(|e| Error::new("OSError", e.to_string()))?;
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|e| Error::new("OSError", e.to_string()))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let mut validated = config.clone();
    validated["dll_sha256"] = Value::String(format!("{:x}", digest.finalize()));
    let dll = crate::ffi::VendorDll::load(family, path)?;
    Ok((validated, Box::new(dll)))
}

pub fn create(family: &str, config: &Value) -> Result<Box<dyn Backend>> {
    #[cfg(feature = "test-backend")]
    if family == "test" {
        return Ok(Box::new(TestBackend {
            state: config.clone(),
        }));
    }
    match family {
        #[cfg(feature = "esi")]
        "esi" => {
            let (config, dll) = vendor(config, family)?;
            Ok(Box::new(crate::esi::Controller::new(&config, dll)?))
        }
        #[cfg(feature = "psu")]
        "psu" => {
            let (config, dll) = vendor(config, family)?;
            Ok(Box::new(crate::psu::Controller::new(&config, dll)?))
        }
        #[cfg(feature = "amx")]
        "amx" => {
            let (config, dll) = vendor(config, family)?;
            Ok(Box::new(crate::amx::Controller::new(&config, dll)?))
        }
        #[cfg(feature = "amx_hd")]
        "amx_hd" => {
            let (config, dll) = vendor(config, family)?;
            Ok(Box::new(crate::amx_hd::Controller::new(&config, dll)?))
        }
        #[cfg(feature = "ampr")]
        "ampr" => {
            let (config, dll) = vendor(config, family)?;
            Ok(Box::new(crate::ampr::Controller::new(&config, dll)?))
        }
        #[cfg(feature = "dmmr")]
        "dmmr" => {
            let (config, dll) = vendor(config, family)?;
            Ok(Box::new(crate::dmmr::Controller::new(&config, dll)?))
        }
        #[cfg(feature = "mscan")]
        "mscan" => Ok(Box::new(crate::mscan::Controller::new(config)?)),
        #[cfg(feature = "transmission")]
        "transmission" => Ok(Box::new(crate::transmission::Controller::new(config)?)),
        #[cfg(feature = "tpg366")]
        "tpg366" => Ok(Box::new(crate::tpg366::Controller::new(config)?)),
        _ => Err(Error::unsupported(format!(
            "Controller {family} is not integrated in this build"
        ))),
    }
}

#[cfg(feature = "test-backend")]
struct TestBackend {
    state: Value,
}

#[cfg(feature = "test-backend")]
impl Backend for TestBackend {
    fn call(
        &mut self,
        method: &str,
        args: &[Value],
        _kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        match method {
            "echo" => Ok(args.first().cloned().unwrap_or(Value::Null)),
            "wait" => {
                ctx.sleep(std::time::Duration::from_secs_f64(
                    args.first().and_then(Value::as_f64).unwrap_or(1.),
                ))?;
                Ok(Value::Bool(true))
            }
            "hang" => loop {
                std::thread::sleep(std::time::Duration::from_secs(60));
            },
            "native_hang" => ctx.native_call("TEST_Vendor_Hang", None, || {
                loop {
                    std::thread::sleep(std::time::Duration::from_secs(60));
                }
            }),
            "native_wait" => ctx.native_call("TEST_Vendor_Wait", None, || {
                std::thread::sleep(std::time::Duration::from_secs_f64(
                    args.first().and_then(Value::as_f64).unwrap_or(0.05),
                ));
                Ok(Value::Bool(true))
            }),
            "crash" => std::process::exit(73),
            "stdout" => {
                println!("native stdout noise");
                Ok(Value::Bool(true))
            }
            "stderr_hang" => {
                use std::io::Write;
                let _ = std::io::stderr().write_all(&vec![b'x'; 1_048_576]);
                loop {
                    std::thread::sleep(std::time::Duration::from_secs(60));
                }
            }
            "progress" => {
                ctx.progress(args.first().cloned().unwrap_or(Value::Null))?;
                Ok(Value::Bool(true))
            }
            "fail" => Err(Error::argument("injected error")),
            _ => Err(Error::unsupported(method)),
        }
    }
    fn get_attribute(&self, name: &str) -> Result<Value> {
        self.state
            .get(name)
            .cloned()
            .ok_or_else(|| Error::new("AttributeError", name))
    }
    fn set_attribute(&mut self, name: &str, value: Value) -> Result<()> {
        self.state[name] = value;
        Ok(())
    }
    fn tick(&mut self, ctx: &Context) -> Result<()> {
        if self.state.get("housekeeping_hang").and_then(Value::as_bool) == Some(true)
            || self
                .state
                .get("housekeeping_stderr_hang")
                .and_then(Value::as_bool)
                == Some(true)
        {
            ctx.native_call(
                "TEST_Vendor_Idle_Hang",
                Some(std::time::Duration::from_millis(50)),
                || {
                    if self
                        .state
                        .get("housekeeping_stderr_hang")
                        .and_then(Value::as_bool)
                        == Some(true)
                    {
                        use std::io::Write;
                        let _ = std::io::stderr().write_all(&vec![b'x'; 1_048_576]);
                    }
                    loop {
                        std::thread::sleep(std::time::Duration::from_secs(60));
                    }
                },
            )?;
        }
        Ok(())
    }
}
