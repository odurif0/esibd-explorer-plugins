use crate::error::{Error, Result};
use serde_json::Value;
use std::path::Path;

#[cfg(feature = "esi")]
#[path = "generated/esi.rs"]
#[rustfmt::skip]
mod esi;
#[cfg(feature = "psu")]
#[path = "generated/psu.rs"]
#[rustfmt::skip]
mod psu;
#[cfg(feature = "amx")]
#[path = "generated/amx.rs"]
#[rustfmt::skip]
mod amx;
#[cfg(feature = "amx_hd")]
#[path = "generated/amx_hd.rs"]
#[rustfmt::skip]
mod amx_hd;
#[cfg(feature = "ampr")]
#[path = "generated/ampr.rs"]
#[rustfmt::skip]
mod ampr;
#[cfg(feature = "dmmr")]
#[path = "generated/dmmr.rs"]
#[rustfmt::skip]
mod dmmr;

#[derive(Debug, Clone)]
pub struct NativeReply {
    pub status: i64,
    pub values: Vec<Value>,
}

pub trait Dll: Send {
    fn call(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply>;
}

pub struct VendorDll {
    library: libloading::Library,
    family: String,
}

impl VendorDll {
    pub fn load(family: &str, path: &Path) -> Result<Self> {
        if !path.is_absolute() || !path.is_file() {
            return Err(Error::argument(
                "Vendor DLL requires an absolute regular-file path",
            ));
        }
        #[cfg(windows)]
        let library =
            unsafe { libloading::os::windows::Library::load_with_flags(path, 0x100 | 0x1000) }
                .map(libloading::Library::from);
        #[cfg(not(windows))]
        let library = unsafe { libloading::Library::new(path) };
        Ok(Self {
            library: library.map_err(|e| Error::new("OSError", e.to_string()))?,
            family: family.to_owned(),
        })
    }
}

impl Dll for VendorDll {
    fn call(&mut self, symbol: &str, args: &[Value]) -> Result<NativeReply> {
        match self.family.as_str() {
            #[cfg(feature = "esi")]
            "esi" => esi::dispatch(&self.library, symbol, args),
            #[cfg(feature = "psu")]
            "psu" => psu::dispatch(&self.library, symbol, args),
            #[cfg(feature = "amx")]
            "amx" => amx::dispatch(&self.library, symbol, args),
            #[cfg(feature = "amx_hd")]
            "amx_hd" => amx_hd::dispatch(&self.library, symbol, args),
            #[cfg(feature = "ampr")]
            "ampr" => ampr::dispatch(&self.library, symbol, args),
            #[cfg(feature = "dmmr")]
            "dmmr" => dmmr::dispatch(&self.library, symbol, args),
            _ => Err(Error::unsupported(
                "DLL family is not compiled into this worker",
            )),
        }
    }
}

pub fn arity(args: &[Value], expected: usize, symbol: &str) -> Result<()> {
    if args.len() != expected {
        return Err(Error::argument(format!(
            "{symbol}: expected {expected} arguments, got {}",
            args.len()
        )));
    }
    Ok(())
}

pub trait NativeScalar: Copy {
    fn parse(value: &Value, boolean: bool) -> Result<Self>;
    fn value(self, boolean: bool) -> Value;
}

macro_rules! integer {
    ($($typ:ty),*) => {$ (
        impl NativeScalar for $typ {
            fn parse(value: &Value, boolean: bool) -> Result<Self> {
                let number = if boolean { value.as_bool().map(i64::from).or_else(|| value.as_i64().filter(|v| *v == 0 || *v == 1)) } else { value.as_i64() };
                number.and_then(|v| <$typ>::try_from(v).ok()).ok_or_else(|| Error::argument(concat!("Invalid or out-of-range ", stringify!($typ))))
            }
            fn value(self, boolean: bool) -> Value {
                if boolean { Value::Bool(self != 0) } else { Value::from(self) }
            }
        }
    )*};
}
integer!(u8, u16, u32, i32);

macro_rules! floating {
    ($($typ:ty),*) => {$ (
        impl NativeScalar for $typ {
            fn parse(value: &Value, _boolean: bool) -> Result<Self> {
                let number = value.as_f64().filter(|v| v.is_finite()).ok_or_else(|| Error::argument("Expected finite native input"))?;
                let converted = number as $typ;
                if !converted.is_finite() { return Err(Error::argument("Floating input exceeds native range")); }
                Ok(converted)
            }
            fn value(self, _boolean: bool) -> Value { crate::codec::float(self as f64) }
        }
    )*};
}
floating!(f32, f64);

pub fn scalar<T: NativeScalar>(value: &Value, boolean: bool) -> Result<T> {
    T::parse(value, boolean)
}

pub fn pointer<T: NativeScalar>(value: &Value, length: usize, boolean: bool) -> Result<Vec<T>> {
    if let Some(values) = value.as_array() {
        if values.len() != length {
            return Err(Error::argument(format!(
                "Native buffer requires {length} elements, not {}",
                values.len()
            )));
        }
        values.iter().map(|v| T::parse(v, boolean)).collect()
    } else if length == 1 {
        Ok(vec![T::parse(value, boolean)?])
    } else {
        Err(Error::argument(format!(
            "Native buffer requires an array of {length} elements"
        )))
    }
}

pub fn pointer_output<T: NativeScalar>(values: &[T], original: &Value, boolean: bool) -> Value {
    if original.is_array() {
        Value::Array(values.iter().map(|v| v.value(boolean)).collect())
    } else {
        values[0].value(boolean)
    }
}

pub fn text_buffer(value: &Value, length: usize) -> Result<Vec<u8>> {
    let text = value
        .get("text")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::argument("Missing native string text"))?;
    if value.get("capacity").and_then(Value::as_u64) != Some(length as u64)
        || text.len() >= length
        || text.contains('\0')
    {
        return Err(Error::argument(format!(
            "Native string requires capacity {length} and a shorter NUL-free value"
        )));
    }
    let mut buffer = vec![0; length];
    buffer[..text.len()].copy_from_slice(text.as_bytes());
    Ok(buffer)
}

pub fn text_output(buffer: &[u8]) -> Value {
    let end = buffer.iter().position(|v| *v == 0).unwrap_or(buffer.len());
    Value::String(String::from_utf8_lossy(&buffer[..end]).into_owned())
}

pub fn text_input(value: &Value, max_length: usize) -> Result<Vec<u8>> {
    let text = value
        .as_str()
        .or_else(|| value.get("text").and_then(Value::as_str))
        .ok_or_else(|| Error::argument("Expected string input"))?;
    if text.len() >= max_length || text.contains('\0') {
        return Err(Error::argument(
            "Native string input too long or contains NUL",
        ));
    }
    let mut bytes = text.as_bytes().to_vec();
    bytes.push(0);
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn buffers_are_fixed_and_boolean_widths_are_distinct() {
        assert_eq!(std::mem::size_of::<u8>(), 1);
        assert_eq!(std::mem::size_of::<i32>(), 4);
        assert!(pointer::<u8>(&json!([false]), 500, true).is_err());
        assert!(pointer::<f64>(&json!(0), 3, false).is_err());
        assert!(scalar::<u8>(&json!(256), false).is_err());
        assert!(scalar::<u8>(&json!(2), true).is_err());
        assert_eq!(pointer_output(&[255u8], &json!(false), true), json!(true));
    }

    #[test]
    fn strings_respect_capacity_and_termination() {
        assert!(text_buffer(&json!({"text":"foo", "capacity":2}), 4).is_err());
        assert_eq!(
            text_buffer(&json!({"text":"foo", "capacity":4}), 4).unwrap(),
            b"foo\0"
        );
        assert_eq!(text_output(b"foo\0junk"), json!("foo"));
    }
}
