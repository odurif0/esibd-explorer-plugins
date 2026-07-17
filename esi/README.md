# ESI Plugin

Controls the CGC ESI electrospray source and monitors HVPS-3kB modules 2 and 3.

The plugin is self-contained and embeds its private driver runtime, vendor
header, and 64-bit Windows DLL.

## Requirements

- ESIBD Explorer `1.0.1`
- Windows for real hardware communication
- CGC ESI controller with HVPS-3kB modules at addresses 2 and 3

## Activation

1. Extract the plugin bundle into the ESIBD Explorer `plugins` folder.
2. Restart ESIBD Explorer.
3. Enable the `ESI` plugin in the Plugin Manager.
4. Configure the Windows COM port.

Initialization validates controller type `0x8ED6`, requires HV modules 2 and 3
to report type `0x0A0D`, and forces both outputs to zero and inactive. Turning
the plugin ON is always an explicit operator action.

Normal target changes and ON/OFF transitions use a configurable software ramp
(500 V/s by default). If a ramp fails, the shutdown path attempts an immediate
zero and deactivation instead.

## Voltage Safety

The HVPS-3kB absolute software limit is 3000 V. Negative targets are disabled
by default because only positive operation has been confirmed on the current
installation. The advanced negative-voltage option must only be enabled after
checking the installed module polarity.

If a DLL call times out or shutdown cannot be confirmed, treat the HV state as
unknown and use the physical interlock or front panel before approaching the
source.

## Portability Note

To copy this plugin to another machine, keep the whole `esi/` directory
together, including the embedded `vendor/` subtree.
