//! Standalone HV-AMX-CTRL-4EDH facade. This is not an AMX parity sibling.
//!
//! The high-level facade and GUI-reachable state live in the AMX lifecycle
//! engine, parameterized with HD ABI/state/configuration rules. This module
//! owns the HD-only low-level facade: indexed oscillator/timers, clocks/PLLs,
//! switches, mapping, digital I/O, interlock, NVM buffers and diagnostics.
//! No operation disables the hardware interlock implicitly.

use crate::Backend;
use crate::amx::{
    self, HD_CONTROLLER_BITS, Params, array, boolean, flags, native_tuple, outputs, text, uint,
};
use crate::codec::tuple;
use crate::context::Context;
use crate::error::{Error, Result};
use crate::ffi::Dll;
use serde_json::{Value, json};

pub struct Controller {
    inner: amx::Controller,
}

impl Controller {
    pub fn new(config: &Value, dll: Box<dyn Dll>) -> Result<Self> {
        Ok(Self {
            inner: amx::Controller::new_family(config, dll, true)?,
        })
    }

    fn raw_status(
        &mut self,
        suffix: &str,
        tail: &[Value],
        pointer_count: usize,
        ctx: &Context,
    ) -> Result<Value> {
        let reply = self.inner.raw(suffix, tail, ctx)?;
        let status = reply.status;
        outputs(reply, pointer_count)?;
        Ok(json!(status))
    }

    fn timer_index(&mut self, value: &Value, ctx: &Context) -> Result<u32> {
        let timer = uint(value, "timer", u32::MAX)?;
        let count = self.inner.read("GetTimerCount", &[json!(0)], 1, ctx)?;
        let count = uint(&count[0], "timer count", 65535)?;
        if timer >= count {
            return Err(Error::argument(format!(
                "Invalid timer {timer}; device reports {count} timers"
            )));
        }
        Ok(timer)
    }

    fn call_hd(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        match method {
            "get_sw_version" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let reply = self.inner.native("GetSWVersion", &[], ctx)?;
                let status = reply.status;
                outputs(reply, 0)?;
                Ok(json!(status))
            }
            "get_state" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let reply = self.inner.raw("GetState", &[json!(0), json!(0)], ctx)?;
                let status = reply.status;
                let values = outputs(reply, 2)?;
                let state = uint(&values[0], "state", u32::MAX)?;
                Ok(tuple(vec![
                    json!(status),
                    json!(format!("0x{state:x}")),
                    values[1].clone(),
                    flags(state, HD_CONTROLLER_BITS, None, None),
                ]))
            }
            "set_config" => {
                let p = Params::new(args, kwargs, &["config"], 1, 1)?;
                self.raw_status("SetConfig", &[json!(uint(&p[0], "config", 65535)?)], 0, ctx)
            }
            "get_interlock_funct" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let reply = self.inner.raw("GetInterlockFunct", &[json!(false)], ctx)?;
                let status = reply.status;
                let values = outputs(reply, 1)?;
                Ok(tuple(vec![
                    json!(status),
                    json!(boolean(&values[0], "interlock")?),
                ]))
            }
            "set_interlock_funct" => {
                let p = Params::new(args, kwargs, &["interlock_funct"], 1, 1)?;
                self.raw_status(
                    "SetInterlockFunct",
                    &[json!(boolean(&p[0], "interlock_funct")?)],
                    0,
                    ctx,
                )
            }
            "get_clock_count"
            | "get_pll_count"
            | "get_divider_count"
            | "get_counter_count"
            | "get_oscillator_count"
            | "get_timer_count" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let suffix = match method {
                    "get_clock_count" => "GetClockCount",
                    "get_pll_count" => "GetPllCount",
                    "get_divider_count" => "GetDividerCount",
                    "get_counter_count" => "GetCounterCount",
                    "get_oscillator_count" => "GetOscillatorCount",
                    _ => "GetTimerCount",
                };
                native_tuple(self.inner.raw(suffix, &[json!(0)], ctx)?, 1)
            }
            "get_clock_source"
            | "get_pll_source"
            | "get_pll_ref_div"
            | "get_pll_fdbk_div"
            | "get_pll_post_div1"
            | "get_pll_post_div2"
            | "get_pll_post_div3"
            | "get_pll_power_down"
            | "get_pll_cp_current"
            | "get_pll_lf_resistor"
            | "get_pll_lf_capacitor"
            | "get_divider_period"
            | "get_counter_period"
            | "get_oscillator_period"
            | "get_fan_speed"
            | "get_digital_io_config"
            | "get_digital_io_signal_source"
            | "get_switch_trigger_source"
            | "get_switch_enable_source"
            | "get_switch_rise_delay_fine"
            | "get_switch_fall_delay_fine" => {
                let (name, suffix, max) = match method {
                    "get_clock_source" => ("clock", "GetClockSource", 3),
                    "get_pll_source" => ("pll", "GetPllSource", 3),
                    "get_pll_ref_div" => ("pll", "GetPllRefDiv", 3),
                    "get_pll_fdbk_div" => ("pll", "GetPllFdbkDiv", 3),
                    "get_pll_post_div1" => ("pll", "GetPllPostDiv1", 3),
                    "get_pll_post_div2" => ("pll", "GetPllPostDiv2", 3),
                    "get_pll_post_div3" => ("pll", "GetPllPostDiv3", 3),
                    "get_pll_power_down" => ("pll", "GetPllPowerDown", 3),
                    "get_pll_cp_current" => ("pll", "GetPllCpCurrent", 3),
                    "get_pll_lf_resistor" => ("pll", "GetPllLfResistor", 3),
                    "get_pll_lf_capacitor" => ("pll", "GetPllLfCapacitor", 3),
                    "get_divider_period" => ("divider", "GetDividerPeriod", 3),
                    "get_counter_period" => ("counter", "GetCounterPeriod", 3),
                    "get_oscillator_period" => ("oscillator", "GetOscillatorPeriod", 3),
                    "get_fan_speed" => ("fan_number", "GetFanSpeed", 2),
                    "get_digital_io_config" => ("digital_io", "GetDigitalIOConfig", 7),
                    "get_digital_io_signal_source" => ("digital_io", "GetDigitalIOSignalSource", 7),
                    "get_switch_trigger_source" => ("switch_no", "GetSwitchTriggerSource", 3),
                    "get_switch_enable_source" => ("switch_no", "GetSwitchEnableSource", 3),
                    "get_switch_rise_delay_fine" => ("switch_no", "GetSwitchRiseDelayFine", 3),
                    _ => ("switch_no", "GetSwitchFallDelayFine", 3),
                };
                let p = Params::new(args, kwargs, &[name], 1, 1)?;
                let index = uint(&p[0], name, max)?;
                let reply = self.inner.raw(
                    suffix,
                    &[
                        json!(index),
                        if method == "get_pll_power_down" {
                            json!(false)
                        } else {
                            json!(0)
                        },
                    ],
                    ctx,
                )?;
                if method == "get_pll_power_down" {
                    let status = reply.status;
                    let values = outputs(reply, 1)?;
                    Ok(tuple(vec![
                        json!(status),
                        json!(boolean(&values[0], "power_down")?),
                    ]))
                } else {
                    native_tuple(reply, 1)
                }
            }
            "set_clock_source"
            | "set_pll_source"
            | "set_pll_ref_div"
            | "set_pll_fdbk_div"
            | "set_pll_post_div1"
            | "set_pll_post_div2"
            | "set_pll_post_div3"
            | "set_pll_power_down"
            | "set_pll_cp_current"
            | "set_pll_lf_resistor"
            | "set_pll_lf_capacitor"
            | "set_divider_period"
            | "set_counter_period"
            | "set_oscillator_period"
            | "set_digital_io_config"
            | "set_digital_io_signal_source"
            | "set_switch_trigger_source"
            | "set_switch_enable_source"
            | "set_switch_rise_delay_fine"
            | "set_switch_fall_delay_fine" => {
                let (index_name, value_name, suffix, index_max, minimum, maximum) = match method {
                    "set_clock_source" => ("clock", "source", "SetClockSource", 3, 0, 143),
                    "set_pll_source" => ("pll", "source", "SetPllSource", 3, 0, 143),
                    "set_pll_ref_div" => ("pll", "divider", "SetPllRefDiv", 3, 1, 4095),
                    "set_pll_fdbk_div" => ("pll", "divider", "SetPllFdbkDiv", 3, 4, 16383),
                    "set_pll_post_div1" => ("pll", "divider", "SetPllPostDiv1", 3, 1, 12),
                    "set_pll_post_div2" => ("pll", "divider", "SetPllPostDiv2", 3, 1, 12),
                    "set_pll_post_div3" => ("pll", "divider", "SetPllPostDiv3", 3, 0, 3),
                    "set_pll_power_down" => ("pll", "power_down", "SetPllPowerDown", 3, 0, 1),
                    "set_pll_cp_current" => ("pll", "cp_current", "SetPllCpCurrent", 3, 0, 3),
                    "set_pll_lf_resistor" => ("pll", "lf_resistor", "SetPllLfResistor", 3, 0, 3),
                    "set_pll_lf_capacitor" => ("pll", "lf_capacitor", "SetPllLfCapacitor", 3, 0, 1),
                    "set_divider_period" => ("divider", "period", "SetDividerPeriod", 3, 0, 255),
                    "set_counter_period" => ("counter", "period", "SetCounterPeriod", 3, 2, 20),
                    "set_oscillator_period" => (
                        "oscillator",
                        "period",
                        "SetOscillatorPeriod",
                        3,
                        0,
                        u32::MAX,
                    ),
                    "set_digital_io_config" => {
                        ("digital_io", "config", "SetDigitalIOConfig", 7, 0, 2)
                    }
                    "set_digital_io_signal_source" => (
                        "digital_io",
                        "signal_source",
                        "SetDigitalIOSignalSource",
                        7,
                        0,
                        255,
                    ),
                    "set_switch_trigger_source" => (
                        "switch_no",
                        "trigger_source",
                        "SetSwitchTriggerSource",
                        3,
                        0,
                        191,
                    ),
                    "set_switch_enable_source" => (
                        "switch_no",
                        "enable_source",
                        "SetSwitchEnableSource",
                        3,
                        0,
                        191,
                    ),
                    "set_switch_rise_delay_fine" => {
                        ("switch_no", "delay", "SetSwitchRiseDelayFine", 3, 0, 511)
                    }
                    _ => ("switch_no", "delay", "SetSwitchFallDelayFine", 3, 0, 511),
                };
                let p = Params::new(args, kwargs, &[index_name, value_name], 2, 2)?;
                let index = uint(&p[0], index_name, index_max)?;
                let value = if method == "set_pll_power_down" {
                    json!(boolean(&p[1], "power_down")?)
                } else {
                    let value = uint(&p[1], value_name, maximum)?;
                    if value < minimum {
                        return Err(Error::argument(format!(
                            "{value_name} must be >= {minimum}"
                        )));
                    }
                    if method == "set_pll_fdbk_div" && [6, 7, 11].contains(&value) {
                        return Err(Error::argument("Unsupported PLL feedback divider"));
                    }
                    if (method == "set_clock_source" || method == "set_pll_source")
                        && value & !0x8f != 0
                    {
                        return Err(Error::argument("Invalid clock/PLL source bits"));
                    }
                    if method.starts_with("set_switch_")
                        && method.ends_with("source")
                        && value & !0xbf != 0
                    {
                        return Err(Error::argument("Invalid switch source bits"));
                    }
                    json!(value)
                };
                self.raw_status(suffix, &[json!(index), value], 0, ctx)
            }
            "get_timer_delay"
            | "get_timer_width"
            | "get_timer_burst"
            | "get_timer_trigger_source"
            | "get_timer_stop_source" => {
                let p = Params::new(args, kwargs, &["timer"], 1, 1)?;
                let timer = self.timer_index(&p[0], ctx)?;
                let suffix = match method {
                    "get_timer_delay" => "GetTimerDelay",
                    "get_timer_width" => "GetTimerWidth",
                    "get_timer_burst" => "GetTimerBurst",
                    "get_timer_trigger_source" => "GetTimerTriggerSource",
                    _ => "GetTimerStopSource",
                };
                native_tuple(self.inner.raw(suffix, &[json!(timer), json!(0)], ctx)?, 1)
            }
            "set_timer_delay"
            | "set_timer_width"
            | "set_timer_burst"
            | "set_timer_trigger_source"
            | "set_timer_stop_source" => {
                let (name, suffix, maximum) = match method {
                    "set_timer_delay" => ("delay", "SetTimerDelay", u32::MAX),
                    "set_timer_width" => ("width", "SetTimerWidth", u32::MAX),
                    "set_timer_burst" => ("burst", "SetTimerBurst", (1 << 24) - 1),
                    "set_timer_trigger_source" => ("trigger_source", "SetTimerTriggerSource", 191),
                    _ => ("stop_source", "SetTimerStopSource", 191),
                };
                let p = Params::new(args, kwargs, &["timer", name], 2, 2)?;
                let value = uint(&p[1], name, maximum)?;
                if method.ends_with("source") && value & !0xbf != 0 {
                    return Err(Error::argument("Invalid timer source bits"));
                }
                let timer = self.timer_index(&p[0], ctx)?;
                self.raw_status(suffix, &[json!(timer), json!(value)], 0, ctx)
            }
            "get_switch_delay" => {
                let p = Params::new(args, kwargs, &["switch_no"], 1, 1)?;
                let number = uint(&p[0], "switch_no", 3)?;
                native_tuple(
                    self.inner
                        .raw("GetSwitchDelay", &[json!(number), json!(0), json!(0)], ctx)?,
                    2,
                )
            }
            "set_switch_delay" => {
                let p = Params::new(
                    args,
                    kwargs,
                    &["switch_no", "rise_delay", "fall_delay"],
                    3,
                    3,
                )?;
                let number = uint(&p[0], "switch_no", 3)?;
                let rise = uint(&p[1], "rise_delay", 15)?;
                let fall = uint(&p[2], "fall_delay", 15)?;
                self.raw_status(
                    "SetSwitchDelay",
                    &[json!(number), json!(rise), json!(fall)],
                    0,
                    ctx,
                )
            }
            "get_mapping_engine_input_source" | "get_mapping_engine_output_value" => {
                let p = Params::new(args, kwargs, &["mapping_engine"], 1, 1)?;
                let number = uint(&p[0], "mapping_engine", u32::MAX)?;
                let (suffix, tail, count) = if method == "get_mapping_engine_input_source" {
                    (
                        "GetMappingEngineInputSource",
                        vec![json!(number), json!(vec![0; 4])],
                        1,
                    )
                } else {
                    (
                        "GetMappingEngineOutputValue",
                        vec![json!(number), json!(false), json!(vec![0; 7])],
                        2,
                    )
                };
                let reply = self.inner.raw(suffix, &tail, ctx)?;
                let status = reply.status;
                let mut values = outputs(reply, count)?;
                let last = values.last().expect("mapping output");
                array(last, if count == 1 { 4 } else { 7 }, "mapping output")?;
                if count == 2 {
                    values[0] = json!(boolean(&values[0], "mapping enable")?);
                }
                values.insert(0, json!(status));
                Ok(tuple(values))
            }
            "set_mapping_engine_input_source" | "set_mapping_engine_output_value" => {
                let output = method == "set_mapping_engine_output_value";
                let names: &[&str] = if output {
                    &["mapping_engine", "enable", "output_values"]
                } else {
                    &["mapping_engine", "input_sources"]
                };
                let p = Params::new(args, kwargs, names, names.len(), names.len())?;
                let number = uint(&p[0], "mapping_engine", u32::MAX)?;
                let values = byte_buffer(
                    &p[if output { 2 } else { 1 }],
                    if output { 7 } else { 4 },
                    if output { 15 } else { 191 },
                )?;
                if !output
                    && values
                        .as_array()
                        .expect("buffer")
                        .iter()
                        .any(|v| v.as_u64().expect("byte") & !0xbf != 0)
                {
                    return Err(Error::argument("Invalid mapping input source bits"));
                }
                let tail = if output {
                    vec![json!(number), json!(boolean(&p[1], "enable")?), values]
                } else {
                    vec![json!(number), values]
                };
                self.raw_status(
                    if output {
                        "SetMappingEngineOutputValue"
                    } else {
                        "SetMappingEngineInputSource"
                    },
                    &tail,
                    1,
                    ctx,
                )
            }
            "get_current_config" | "get_defaults" | "get_signal_values" | "get_signals" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let (suffix, size, is_bool) = match method {
                    "get_current_config" => ("GetCurrentConfig", 318, false),
                    "get_defaults" => ("GetDefaults", 20, false),
                    "get_signal_values" => ("GetSignalValues", 64, true),
                    _ => ("GetSignals", 8, false),
                };
                let reply = self.inner.raw(
                    suffix,
                    &[if is_bool {
                        json!(vec![false; size])
                    } else {
                        json!(vec![0; size])
                    }],
                    ctx,
                )?;
                let status = reply.status;
                let values = outputs(reply, 1)?;
                let row = array(&values[0], size, method)?;
                let row = if is_bool {
                    json!(
                        row.iter()
                            .map(|v| boolean(v, "signal"))
                            .collect::<Result<Vec<_>>>()?
                    )
                } else {
                    values[0].clone()
                };
                Ok(tuple(vec![json!(status), row]))
            }
            "set_current_config" | "set_defaults" => {
                let p = Params::new(args, kwargs, &["data"], 1, 1)?;
                let data = byte_buffer(
                    &p[0],
                    if method == "set_current_config" {
                        318
                    } else {
                        20
                    },
                    255,
                )?;
                self.raw_status(
                    if method == "set_current_config" {
                        "SetCurrentConfig"
                    } else {
                        "SetDefaults"
                    },
                    &[data],
                    1,
                    ctx,
                )
            }
            "get_config_data" | "set_config_data" | "set_config_name" | "set_config_flags" => {
                let names: &[&str] = match method {
                    "get_config_data" => &["config_number"],
                    "set_config_data" => &["config_number", "data"],
                    "set_config_name" => &["config_number", "name"],
                    _ => &["config_number", "active", "valid"],
                };
                let p = Params::new(args, kwargs, names, names.len(), names.len())?;
                let number = self.inner.config_number(&p[0])?;
                match method {
                    "get_config_data" => {
                        let reply = self.inner.raw(
                            "GetConfigData",
                            &[json!(number), json!(vec![0; 318])],
                            ctx,
                        )?;
                        let status = reply.status;
                        let values = outputs(reply, 1)?;
                        array(&values[0], 318, "config data")?;
                        Ok(tuple(vec![json!(status), values[0].clone()]))
                    }
                    "set_config_data" => {
                        let data = byte_buffer(&p[1], 318, 255)?;
                        self.raw_status("SetConfigData", &[json!(number), data], 1, ctx)
                    }
                    "set_config_name" => {
                        let name = p[1]
                            .as_str()
                            .ok_or_else(|| Error::argument("name must be a string"))?;
                        if name.len() >= 193 || name.contains('\0') {
                            return Err(Error::argument(
                                "Config name must fit in 193 UTF-8 bytes including NUL",
                            ));
                        }
                        self.raw_status(
                            "SetConfigName",
                            &[json!(number), json!({"capacity": 193, "text": name})],
                            1,
                            ctx,
                        )
                    }
                    _ => self.raw_status(
                        "SetConfigFlags",
                        &[
                            json!(number),
                            json!(boolean(&p[1], "active")?),
                            json!(boolean(&p[2], "valid")?),
                        ],
                        0,
                        ctx,
                    ),
                }
            }
            "save_defaults" | "load_defaults" | "restart" | "get_interface_state" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let suffix = match method {
                    "save_defaults" => "SaveDefaults",
                    "load_defaults" => "LoadDefaults",
                    "restart" => "Restart",
                    _ => "GetInterfaceState",
                };
                self.raw_status(suffix, &[], 0, ctx)
            }
            "get_manuf_date" | "get_cpu_id" | "get_dev_type" | "get_io_state"
            | "get_comm_error" => {
                Params::new(args, kwargs, &[], 0, 0)?;
                let (suffix, count) = match method {
                    "get_manuf_date" => ("GetManufDate", 2),
                    "get_cpu_id" => ("GetCPU_ID", 1),
                    "get_dev_type" => ("GetDevType", 1),
                    "get_io_state" => ("GetIOState", 1),
                    _ => ("GetCommError", 1),
                };
                native_tuple(self.inner.raw(suffix, &vec![json!(0); count], ctx)?, count)
            }
            "get_error_message"
            | "get_io_error_message"
            | "get_io_state_message"
            | "get_comm_error_message" => {
                let (suffix, names) = match method {
                    "get_error_message" => ("GetErrorMessage", Vec::<&str>::new()),
                    "get_io_error_message" => ("GetIOErrorMessage", vec![]),
                    "get_io_state_message" => ("GetIOStateMessage", vec!["io_state"]),
                    _ => ("GetCommErrorMessage", vec!["comm_error"]),
                };
                let p = Params::new(args, kwargs, &names, names.len(), names.len())?;
                // These exports return char*, encoded by the ABI bridge as
                // status=0 plus one copied string. Message helpers have no stream.
                let reply = if method == "get_io_state_message" {
                    let number = p[0]
                        .as_i64()
                        .filter(|v| i32::try_from(*v).is_ok())
                        .ok_or_else(|| Error::argument("io_state must fit in int32"))?;
                    self.inner.native(suffix, &[json!(number)], ctx)?
                } else if method == "get_comm_error_message" {
                    self.inner.native(
                        suffix,
                        &[json!(uint(&p[0], "comm_error", u32::MAX)?)],
                        ctx,
                    )?
                } else {
                    self.inner.raw(suffix, &[], ctx)?
                };
                let values = outputs(reply, 1)?;
                let message = text(&values[0], "diagnostic message")?;
                Ok(json!(if message.is_empty() {
                    "No error".to_owned()
                } else {
                    message
                }))
            }
            _ => self.inner.call_common(method, args, kwargs, ctx),
        }
    }
}

impl Backend for Controller {
    fn call(
        &mut self,
        method: &str,
        args: &[Value],
        kwargs: &Value,
        ctx: &Context,
    ) -> Result<Value> {
        self.inner.begin_call();
        ctx.check_cancelled()?;
        let result = self.call_hd(method, args, kwargs, ctx);
        self.inner.finish_call(result, ctx)
    }

    fn get_attribute(&self, name: &str) -> Result<Value> {
        let value =
            match name {
                "CONFIG_DATA_SIZE" => json!(318),
                "DEFAULT_DATA_SIZE" => json!(20),
                "TIMER_MAX_BURST" => json!(1 << 24),
                "TIMER_MASK_BURST" => json!((1 << 24) - 1),
                "SWITCH_DELAY_SIZE" => json!(4),
                "SWITCH_DELAY_MASK" => json!(15),
                "SWITCH_DELAY_SCALE" => json!(5e-9),
                "SWITCH_DELAY_FINE_MAX" => json!(512),
                "SWITCH_DELAY_FINE_SCALE" => json!(11e-12),
                "MAPPING_INPUT_NUM" => json!(4),
                "MAPPING_STATE_NUM" => json!(7),
                "MAPPING_STATE_BITS" => json!(15),
                "SIGNAL_NUM" => json!(64),
                "SIGNAL_MASK" => json!(63),
                "SIGNAL_INVERT" => json!(128),
                "SOURCE_MASK" => json!(191),
                "SIGNAL_NUM_BYTE" => json!(8),
                "CLOCK_NUM" | "PLL_NUM" | "DIV_NUM" | "CNT_NUM" => json!(4),
                "CLOCK_SRC_NUM" | "PLL_SRC_NUM" => json!(16),
                "CLOCK_SRC_MASK" | "PLL_SRC_MASK" => json!(15),
                "CLOCK_SRC_INVERT" | "PLL_SRC_INVERT" => json!(128),
                "CLOCK_SOURCE_MASK" | "PLL_SOURCE_MASK" => json!(143),
                "PLL_REF_DIV_MIN" | "PLL_POST_DIV1_MIN" | "PLL_POST_DIV2_MIN" => json!(1),
                "PLL_REF_DIV_MAX" => json!(4095),
                "PLL_FB_DIV_MIN" => json!(4),
                "PLL_FB_DIV_MAX" => json!(16383),
                "PLL_FB_DIV_NA1" => json!(6),
                "PLL_FB_DIV_NA2" => json!(7),
                "PLL_FB_DIV_NA3" => json!(11),
                "PLL_POST_DIV1_MAX" | "PLL_POST_DIV2_MAX" => json!(12),
                "PLL_POST_DIV3_1" | "PLL_CP_2u0" | "PLL_LR_400K" | "PLL_LC_185pF"
                | "DIO_CFG_IN" => json!(0),
                "PLL_POST_DIV3_2" | "PLL_CP_4u5" | "PLL_LR_133K" | "PLL_LC_500pF"
                | "DIO_CFG_OUT" => json!(1),
                "PLL_POST_DIV3_4" | "PLL_CP_11u0" | "PLL_LR_30K" | "DIO_CFG_IN_TERM" => json!(2),
                "PLL_POST_DIV3_8" | "PLL_CP_22u5" | "PLL_LR_12K" => json!(3),
                "PLL_POST_DIV3_MAX" | "PLL_CP_MAX" | "PLL_LR_MAX" => json!(4),
                "PLL_LC_MAX" => json!(2),
                "DIV_CLOCK" | "CNT_CLOCK" => json!(200e6),
                "DIV_OFFSET" | "CNT_PER_MIN" => json!(2),
                "CNT_PER_MAX" => json!(20),
                "DIO_NUM" => json!(8),
                "DIO_SRC_NUM" => json!(80),
                "DIO_SRC_MASK" => json!(127),
                "DIO_SRC_INVERT" => json!(128),
                "DIO_SOURCE_MASK" => json!(255),
                "CPU_ID" => json!(0x5a3a),
                "DEVICE_TYPE" => json!(0x0b21),
                _ => return self.inner.attribute(name),
            };
        Ok(value)
    }

    fn set_attribute(&mut self, name: &str, value: Value) -> Result<()> {
        self.inner.assign_attribute(name, value)
    }
}

fn byte_buffer(value: &Value, size: usize, maximum: u32) -> Result<Value> {
    let value = value.get("$bytes").unwrap_or(value);
    let values = value
        .as_array()
        .ok_or_else(|| Error::argument("Expected a fixed-length byte buffer"))?;
    if values.len() != size {
        return Err(Error::argument(format!(
            "Buffer must contain exactly {size} bytes"
        )));
    }
    Ok(json!(
        values
            .iter()
            .map(|v| uint(v, "buffer byte", maximum))
            .collect::<Result<Vec<_>>>()?
    ))
}
