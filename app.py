from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from pymodbus.client import ModbusSerialClient
import json

# ---- Load configuration ----
with open("config.json", "r") as f:
    CFG = json.load(f)

app = Flask(__name__)
app.secret_key = "vfd-hmi"

# ---- Modbus client factory ----
def mclient():
    return ModbusSerialClient(
        port=CFG["serial_port"],
        baudrate=CFG["baudrate"],
        parity=CFG["parity"],
        stopbits=CFG["stopbits"],
        bytesize=CFG["bytesize"],
        timeout=CFG["timeout"]
    )

# ---- Utilities ----
def clamp_hz(hz: float) -> float:
    lo, hi = CFG["limits_hz"]["min"], CFG["limits_hz"]["max"]
    return max(lo, min(hi, float(hz)))

def pct10k_from_hz(hz: float) -> int:
    # 0x1000 expects -10000..10000 -> -100..100% of P0-10 (base)
    base = float(CFG["base_freq_hz"])
    return int(round((hz / base) * 10000.0))

# ---- Modbus helpers (pymodbus 3.x uses keyword-only args) ----
def read_regs(addr, count=1):
    c = mclient()
    try:
        if not c.connect():
            return None
        rr = c.read_holding_registers(address=addr, count=count, device_id=CFG["unit_id"])
        if rr is None or rr.isError():
            return None
        return rr.registers
    except Exception:
        return None
    finally:
        try: c.close()
        except: pass

def write_reg(addr, value) -> bool:
    c = mclient()
    try:
        if not c.connect():
            return False
        rq = c.write_register(address=addr, value=value, device_id=CFG["unit_id"])
        return (rq is not None) and (not rq.isError())
    except Exception:
        return False
    finally:
        try: c.close()
        except: pass

# ---- Data reads ----
def read_freq_cmd():
    regs = read_regs(CFG["regs"]["freq_set"], 1)
    if regs is None:
        return None
    pct10k = regs[0]             # -10000..10000 = -100..100 % of base
    return (pct10k / 10000.0) * float(CFG["base_freq_hz"])


def read_freq_hz():
    regs = read_regs(CFG["regs"]["freq_fb"], 1)
    if regs is None:
        return None
    return regs[0] / 100.0   # feedback is in 0.01 Hz

def read_current_a():
    regs = read_regs(CFG["regs"]["current_fb"], 1)
    if regs is None:
        return None
    raw = regs[0]
    # Heuristic scaling: many drives use 0.01 A units
    return raw / 100.0 if raw > 200 else float(raw)

def read_status_text():
    regs = read_regs(CFG["regs"]["status"], 1)
    if regs is None: return "No comm"
    return {1: "RUN FWD", 2: "RUN REV", 3: "STOP"}.get(regs[0], f"STATE {regs[0]}")

def read_fault():
    regs = read_regs(CFG["regs"]["fault"], 1)
    if regs is None: return None
    return regs[0]  # 0 = no fault

# ---- Web routes ----
@app.route("/api/status")
def api_status():
    data = {
        "status": read_status_text(),
        "fault": read_fault(),
        "freq_cmd": read_freq_cmd(),
        "freq_act": read_freq_hz(),
        "amps": read_current_a()
    }
    return jsonify(data)
@app.route("/")
def index():
    freq_cmd = read_freq_cmd()          # commanded Hz
    freq_act = read_freq_hz()           # actual Hz
    amps     = read_current_a()
    ratedA   = float(CFG.get("rated_current_a", 10.0))

    # default slider at maxHz if no readback
    sliderHz = freq_cmd if freq_cmd is not None else CFG["limits_hz"]["max"]

    return render_template(
        "index.html",
        freq_cmd=freq_cmd,
        freq_act=freq_act,
        amps=amps,
        ratedA=ratedA,
        status=read_status_text(),
        fault=read_fault(),
        minHz=CFG["limits_hz"]["min"],
        maxHz=CFG["limits_hz"]["max"],
        sliderHz=sliderHz
    )




@app.route("/start", methods=["POST"])
def start():
    ok = write_reg(CFG["regs"]["cmd"], 1)  # forward run
    flash("START " + ("OK" if ok else "FAILED"))
    return redirect(url_for("index"))

@app.route("/stop", methods=["POST"])
def stop():
    ok = write_reg(CFG["regs"]["cmd"], 6)  # slow stop
    flash("STOP " + ("OK" if ok else "FAILED"))
    return redirect(url_for("index"))

@app.route("/reset", methods=["POST"])
def reset():
    ok = write_reg(CFG["regs"]["cmd"], 7)  # fault reset
    flash("RESET " + ("OK" if ok else "FAILED"))
    return redirect(url_for("index"))

@app.route("/setfreq", methods=["POST"])
def setfreq():
    try:
        hz = float(request.form["freq"])
    except Exception:
        flash("Invalid frequency")
        return redirect(url_for("index"))
    hz = clamp_hz(hz)                     # enforce 25–50 Hz
    value = pct10k_from_hz(hz)            # convert to -10000..10000
    ok = write_reg(CFG["regs"]["freq_set"], value)
    flash((f"Set {hz:.2f} Hz (cmd={value})") if ok else "Write failed")
    return redirect(url_for("index"))

if __name__ == "__main__":
    # single-threaded avoids serial port contention
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=False)
