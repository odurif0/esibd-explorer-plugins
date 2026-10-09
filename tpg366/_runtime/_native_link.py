"""TPG366 data/exception adapter; the isolated native worker owns the USB port."""
from __future__ import annotations

class NativeLink:
    def __init__(self, worker_class, protocol, plugin_root, port, baudrate, cancelled, *, log_dir=None):
        self.protocol, self.cancelled = protocol, cancelled
        self.proxy = worker_class(plugin_root, "tpg366", {"port": port, "baudrate": baudrate}, log_dir=log_dir)
        self.identification, self.gauges, self.unit, self.nak_count = "", (), None, 0

    @property
    def is_open(self):
        process = getattr(self.proxy, '_process', None)
        if process is not None:
            return process.poll() is None
        return not self.proxy.closed

    def _progress(self, report):
        if isinstance(report, dict) and report.get('family') == 'tpg366':
            count = report.get('nak_count')
            if type(count) is int and count >= self.nak_count:
                self.nak_count = count

    def _call(self, method, timeout=5.):
        if self.cancelled.is_set():
            raise self.protocol.Cancelled
        try:
            result = self.proxy.call_method(method, rpc_timeout_s=timeout,
                                            _cancel_event=self.cancelled, on_progress=self._progress)
            if self.cancelled.is_set():
                raise self.protocol.Cancelled
            return result
        except Exception as exc:
            if self.cancelled.is_set():
                raise self.protocol.Cancelled from exc
            if getattr(exc, "native_error_kind", "") == "ProtocolError":
                raise self.protocol.ProtocolError(str(exc)) from exc
            raise

    def initialize(self):
        identity = self._call("initialize", timeout=10.)
        self.identification, self.gauges, self.unit = identity["identification"], identity["gauges"], identity["unit"]

    def read_pressures(self):
        reading = self._call("read_pressures", timeout=5.)
        return self.protocol.Reading(**reading)

    def close(self):
        if self.proxy.close(grace_s=0) is False or self.is_open:
            raise OSError('Native TPG366 worker exit/USB port release is unconfirmed.')

    def cancel(self):
        self.proxy.cancel()
