#!/usr/bin/env python3
"""
Analisador de energia simplificado p/ ensaio grid-zero (proc. 06).
Notebook + medidor Modbus = tela com valores ao vivo, grafico P(t) e CSV.
Somente biblioteca padrao (tkinter) + pymodbus (+ pyserial p/ RTU).

Fontes suportadas:
  [EMS-TCP]  EMS CPS C 1000 via Ethernet: 0x0106, FC04, int32BE, x0.1 kW.
  [DTSU-RTU] DTSU666 dedicado via conversor RS485-USB: registrador
             descoberto no modo scan do script 09 (float32 ABCD x CT ratio).
  [FD2-RTU]  FD2-M1 fora da EdgeBox (A+/B- no conversor, gateway EWX2 fora):
             mesmo modo do DTSU; confirmar endereco/baud/mapa no manual
             da EdgeBox ou com o time de M&C antes.

Requisitos: pip install pymodbus pyserial
Uso: python 10-analisador-energia.py
"""

import csv
import queue
import struct
import threading
import time
import tkinter as tk
from datetime import datetime

TARGET_HZ = 5.0
CHART_SECONDS = 60


def decode(regs, kind):
    if kind == "float32":
        return struct.unpack(">f", struct.pack(">HH", regs[0], regs[1]))[0]
    v = (regs[0] << 16) | regs[1]
    if v >= 0x80000000:
        v -= 0x100000000
    return float(v)


class Reader(threading.Thread):
    def __init__(self, cfg, out_q, stop_ev):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = out_q
        self.stop = stop_ev

    def run(self):
        c = self.cfg
        try:
            if c["mode"] == "EMS-TCP":
                from pymodbus.client import ModbusTcpClient
                client = ModbusTcpClient(c["host"], port=int(c["port"]))
                addr, n, fc, kind, scale = 0x0106, 2, 4, "int32", 0.1
            else:
                from pymodbus.client import ModbusSerialClient
                client = ModbusSerialClient(port=c["port"], baudrate=int(c["baud"]),
                                            parity="N", stopbits=1, bytesize=8, timeout=0.5)
                addr = int(c["reg"], 16)
                n, fc, kind = 2, int(c["fcode"]), c["decode"]
                scale = float(c["ct"])
            if not client.connect():
                self.q.put(("err", "sem conexao"))
                return
        except Exception as e:
            self.q.put(("err", str(e)))
            return
        period = 1.0 / TARGET_HZ
        while not self.stop.is_set():
            t0 = time.monotonic()
            ts = datetime.now().isoformat(timespec="milliseconds")
            try:
                kw = {"address": addr, "count": n}
                try:
                    if fc == 4:
                        rr = client.read_input_registers(device_id=int(c.get("unit", 1)), **kw)
                    else:
                        rr = client.read_holding_registers(device_id=int(c.get("unit", 1)), **kw)
                except TypeError:
                    if fc == 4:
                        rr = client.read_input_registers(unit=int(c.get("unit", 1)), **kw)
                    else:
                        rr = client.read_holding_registers(unit=int(c.get("unit", 1)), **kw)
                if rr.isError():
                    raise IOError(str(rr))
                self.q.put(("val", ts, decode(rr.registers, kind) * scale))
            except Exception as e:
                self.q.put(("err", str(e)[:100]))
            dt = period - (time.monotonic() - t0)
            if dt > 0:
                time.sleep(dt)
        client.close()


class App:
    def __init__(self, root):
        self.root = root
        root.title("Analisador grid-zero (5 Hz)")
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.reader = None
        self.csv = None
        self.w = None
        self.data = []
        self.marks = []

        f = tk.Frame(root)
        f.pack(padx=8, pady=6)
        self.mode = tk.StringVar(value="EMS-TCP")
        tk.OptionMenu(f, self.mode, "EMS-TCP", "DTSU-RTU", "FD2-RTU").grid(row=0, column=0)
        self.e_host = self._ent(f, "host/IP:", "192.168.1.10", 0, 1)
        self.e_com = self._ent(f, "COM/USB:", "COM4", 0, 3)
        self.e_reg = self._ent(f, "reg(hex):", "0x0106", 0, 5)
        self.e_ct = self._ent(f, "CT ratio:", "1", 1, 1)
        self.e_baud = self._ent(f, "baud:", "19200", 1, 3)
        self.e_unit = self._ent(f, "unit:", "1", 1, 5)

        b = tk.Frame(root)
        b.pack(pady=4)
        tk.Button(b, text="Conectar+Iniciar", command=self.start).pack(side="left", padx=4)
        tk.Button(b, text="Parar", command=self.halt).pack(side="left", padx=4)
        tk.Button(b, text="MARCAR EVENTO (degrau)", command=self.mark,
                  bg="#ffd54f").pack(side="left", padx=4)

        self.lbl = tk.Label(root, text="P = ---- kW", font=("Arial", 34, "bold"))
        self.lbl.pack()
        self.lbl2 = tk.Label(root, text="aguardando...", font=("Arial", 11))
        self.lbl2.pack()
        self.cv = tk.Canvas(root, width=760, height=260, bg="white")
        self.cv.pack(padx=8, pady=6)
        self.root.after(200, self.tick)

    def _ent(self, f, lab, default, r, c):
        tk.Label(f, text=lab).grid(row=r, column=c, sticky="e")
        e = tk.Entry(f, width=14)
        e.insert(0, default)
        e.grid(row=r, column=c + 1, sticky="w")
        return e

    def start(self):
        if self.reader and self.reader.is_alive():
            return
        m = self.mode.get()
        if m == "EMS-TCP":
            cfg = {"mode": m, "host": self.e_host.get(), "port": 502, "unit": self.e_unit.get()}
        else:
            cfg = {"mode": "RTU", "port": self.e_com.get(), "baud": self.e_baud.get(),
                   "unit": self.e_unit.get(), "reg": self.e_reg.get(), "fcode": 4,
                   "decode": "float32", "ct": self.e_ct.get()}
        fn = "analisador_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv = open(fn, "w", newline="")
        self.w = csv.writer(self.csv)
        self.w.writerow(["timestamp_iso", "potencia_kw", "evento"])
        self.fn = fn
        self.stop.clear()
        self.reader = Reader(cfg, self.q, self.stop)
        self.reader.start()
        self.lbl2.config(text="registrando em " + fn)

    def halt(self):
        self.stop.set()
        if self.csv:
            self.csv.close()
            self.csv = None
        self.lbl2.config(text="parado.")

    def mark(self):
        self.marks.append(time.monotonic())
        if self.w:
            self.w.writerow([datetime.now().isoformat(timespec="milliseconds"), "", "DEGRAU"])
        self.lbl2.config(text="evento marcado!")

    def tick(self):
        got = False
        while not self.q.empty():
            msg = self.q.get()
            if msg[0] == "val":
                _, ts, p = msg
                self.data.append((time.monotonic(), p))
                if self.w:
                    self.w.writerow([ts, "%.3f" % p, ""])
                self.lbl.config(text="P = %.2f kW" % p,
                                fg="green" if p >= 0 else "red")
                got = True
            else:
                self.lbl2.config(text=msg[1])
        if got:
            now = time.monotonic()
            self.data = [(t, p) for t, p in self.data if now - t < CHART_SECONDS]
            self.draw()
            hz = len(self.data) / max(1, CHART_SECONDS if len(self.data) > 10 else (self.data[-1][0] - self.data[0][0] + 0.01))
            self.lbl2.config(text="%d amostras | ~%.1f Hz | +importa / -exporta" % (len(self.data), min(hz, 20)))
        self.root.after(200, self.tick)

    def draw(self):
        cv = self.cv
        cv.delete("all")
        W, H = 760, 260
        if len(self.data) < 2:
            return
        vals = [p for _, p in self.data]
        lo, hi = min(min(vals), 0), max(max(vals), 1)
        span = (hi - lo) or 1
        t0 = self.data[0][0]

        def X(t):
            return (t - t0) / CHART_SECONDS * W

        def Y(p):
            return H - (p - lo) / span * (H - 20) - 10
        zy = Y(0)
        cv.create_line(0, zy, W, zy, fill="gray", dash=(4, 4))
        pts = []
        for t, p in self.data:
            pts += [X(t), Y(p)]
        cv.create_line(*pts, fill="blue", width=2)
        cv.create_text(60, 12, text="max %.1f kW" % hi, anchor="w")
        cv.create_text(60, zy - 10, text="zero", anchor="w")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
