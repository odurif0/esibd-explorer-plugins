use crate::error::{Error, Result};
use serde_json::Value;
use std::io::{Read, Write};

pub const VERSION: u64 = 1;
pub const MAX_FRAME: usize = 16 * 1024 * 1024;

pub fn read_frame(reader: &mut impl Read) -> Result<Value> {
    let mut header = [0u8; 4];
    reader
        .read_exact(&mut header)
        .map_err(|e| Error::new("EOFError", e.to_string()))?;
    let length = u32::from_be_bytes(header) as usize;
    if length == 0 || length > MAX_FRAME {
        return Err(Error::argument("Frame size outside protocol bounds"));
    }
    let mut data = vec![0u8; length];
    reader
        .read_exact(&mut data)
        .map_err(|e| Error::new("EOFError", e.to_string()))?;
    let value: Value = serde_json::from_slice(&data).map_err(|e| Error::argument(e.to_string()))?;
    if !value.is_object()
        || value.get("version").and_then(Value::as_u64) != Some(VERSION)
        || value.get("id").and_then(Value::as_u64).is_none()
    {
        return Err(Error::argument(
            "Invalid frame object, protocol version or request id",
        ));
    }
    Ok(value)
}

pub fn write_frame(writer: &mut impl Write, value: &Value) -> Result<()> {
    let data = serde_json::to_vec(value).map_err(|e| Error::runtime(e.to_string()))?;
    if data.is_empty() || data.len() > MAX_FRAME {
        return Err(Error::argument("Frame size outside protocol bounds"));
    }
    writer
        .write_all(&(data.len() as u32).to_be_bytes())
        .and_then(|_| writer.write_all(&data))
        .and_then(|_| writer.flush())
        .map_err(|e| Error::new("BrokenPipeError", e.to_string()))
}

pub fn reply(id: u64, result: Result<Value>) -> Value {
    match result {
        Ok(value) => {
            serde_json::json!({"version":VERSION,"id":id,"kind":"reply","success":true,"value":value})
        }
        Err(error) => {
            serde_json::json!({"version":VERSION,"id":id,"kind":"reply","success":false,"error":error})
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn roundtrip_and_bounds() {
        let value = json!({"version":1,"id":0,"op":"init"});
        let mut bytes = Vec::new();
        write_frame(&mut bytes, &value).unwrap();
        assert_eq!(read_frame(&mut &bytes[..]).unwrap(), value);
        assert!(read_frame(&mut &(MAX_FRAME as u32 + 1).to_be_bytes()[..]).is_err());
        let mut invalid = Vec::new();
        write_frame(&mut invalid, &json!({"version":2,"id":0})).unwrap();
        assert!(read_frame(&mut &invalid[..]).is_err());
    }
}
