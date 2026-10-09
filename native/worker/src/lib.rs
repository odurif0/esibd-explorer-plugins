pub mod codec;
pub mod context;
pub mod error;
pub mod factory;
pub mod ffi;
pub mod protocol;
pub const ERROR_CATALOG: &str = include_str!("generated/error_codes.json");
#[cfg(feature = "ampr")]
pub mod ampr;
#[cfg(any(feature = "amx", feature = "amx_hd"))]
pub mod amx;
#[cfg(feature = "amx_hd")]
pub mod amx_hd;
#[cfg(feature = "dmmr")]
pub mod dmmr;
#[cfg(feature = "esi")]
pub mod esi;
#[cfg(feature = "mscan")]
pub mod mscan;
#[cfg(feature = "psu")]
pub mod psu;
#[cfg(feature = "tpg366")]
pub mod tpg366;
#[cfg(feature = "transmission")]
pub mod transmission;

use context::Context;
use error::{Error, Result};
use serde_json::Value;

pub trait Backend: Send {
    fn tick(&mut self, _ctx: &context::Context) -> error::Result<()> {
        Ok(())
    }
    fn call(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value>;

    fn get_attribute(&self, name: &str) -> Result<Value> {
        Err(Error::new(
            "AttributeError",
            format!("Unknown attribute: {name}"),
        ))
    }

    fn set_attribute(&mut self, name: &str, _value: Value) -> Result<()> {
        Err(Error::new(
            "AttributeError",
            format!("Read-only or unknown attribute: {name}"),
        ))
    }
}
