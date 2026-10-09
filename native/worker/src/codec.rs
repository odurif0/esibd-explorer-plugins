use serde_json::{Value, json};

pub fn float(value: f64) -> Value {
    if value.is_finite() {
        json!(value)
    } else {
        json!({"$float": if value.is_nan() { "nan" } else if value.is_sign_positive() { "inf" } else { "-inf" }})
    }
}

pub fn tuple(values: Vec<Value>) -> Value {
    json!({"$tuple": values})
}
