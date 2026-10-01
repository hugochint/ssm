# --------------------------------------------------------------
# SMM – Smart Meter Master (web UI + Modbus RTU)
# --------------------------------------------------------------
import json, os, csv, threading, time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
from pymodbus.client import ModbusSerialClient
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.constants import Endian

app = Flask(__name__)

# ------------------------------------------------------------------
# Configuração global (gravada/liida do ficheiro smc_cfg.json)
# ------------------------------------------------------------------
CFG_PATH = os.path.join(os.path.dirname(__file__), "smc_cfg.json")
DEFAULT_CFG = {
    "port": "COM3",          # ajuste depois que ligar o conversor
    "baudrate": 19200,
    "unit": 1,
    "reg_start": 0x0106,    # registrador inicial (potência ativa)
    "reg_count": 2,         # número de registradores a ler (int32)
    "decode": "int32",      # "int32" ou "float32"
    "scale": 0.1,           # fator de escala (kW → kW, se for int32×0.1)
}
if not os.path.exists(CFG_PATH):
    with open(CFG_PATH, "w") as f:
        json.dump(DEFAULT_CFG, f, indent=2)

with open(CFG_PATH) as f:
    CFG = json.load(f)

# ------------------------------------------------------------------
# Estado partilhado entre threads
# ------------------------------------------------------------------
state = {
    "last_values": {},   # { "kW": ..., "V": ..., "A": ..., "PF": ... }
    "stop_event": threading.Event(),
    "csv_file": None,
    "csv_writer": None,
}
# ------------------------------------------------------------------
# Função de leitura Modbus (rodando em thread separada)
# ------------------------------------------------------------------
def modbus_reader():
    c = CFG
    client = ModbusSerialClient(
        port=c["port"],
        baudrate=c["baudrate"],
        timeout=0.5,
        bytesize=8,
        parity="N",
        stopbits=1,
    )
    if not client.connect():
        app.logger.error("Não foi possível abrir a porta %s", c["port"])
        return

    addr = c["reg_start"]
    count = c["reg_count"]
    scale = c["scale"]

    # Abre CSV para escrita (append)
    fn = f"smc_data_{datetime.now().strftime('%Y%m%d')}.csv"
    csv_file = open(fn, "a", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["timestamp", "kW", "evento"])

    regs = [0, 0]  # buffer para os dois registradores

    while not state["stop_event"].is_set():
        try:
            # Leitura de Holding Registers (0x03) – o FD2‑M1/DTSU usa este tipo
            rr = client.read_holding_registers(
                address=addr, unit_id=c["unit"], count=count
            )
            if rr.isError():
                raise IOError(str(rr))

            # Decodifica dependendo do tipo escolhido
            decoder = BinaryPayloadDecoder.fromRegisters(
                rr.registers, byteorder=Endian.Big, wordorder=Endian.Big
            )
            if c["decode"] == "int32":
                raw = decoder.decode_32bit_int()   # inteiro signed
                value = raw * scale
            else:  # float32
                raw = decoder.decode_32bit_float()
                value = raw

            # Atualiza dicionário partilhado (thread‑safe porque só lemos)
            state["last_values"]["kW"] = value

            # Grava linha no CSV (sem evento = "")
            ts = datetime.now().isoformat(timespec="milliseconds")
            writer.writerow([ts, f"{value:.3f}", ""])
            csv_file.flush()   # garante escrita inmediata

        except Exception as e:
            app.logger.exception("Erro na leitura Modbus: %s", e)
            state["last_values"]["kW"] = None
            time.sleep(0.5)  # back‑off antes de tentar novamente

    # Fim de loop – fecha tudo
    client.close()
    csv_file.close()


# ------------------------------------------------------------------
# Rotas HTTP
# ------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/values")
def api_values():
    """Retorna JSON com os últimos valores lidos."""
    vals = state["last_values"]
    return jsonify(vals)


@app.route("/mark-event", methods=["POST"])
def mark_event():
    """Recebe POST de 'Marcar evento' do JS e grava linha no CSV."""
    if state["csv_writer"] is None:
        return "CSV not open", 500
    ts = datetime.now().isoformat(timespec="milliseconds")
    state["csv_writer"].writerow([ts, "", "DEGRAU"])
    state["csv_writer"].flush()
    return "OK"


@app.route("/export-csv")
def export_csv():
    """Força o download do CSV do dia atual."""
    fn = f"smc_data_{datetime.now().strftime('%Y%m%d')}.csv"
    if os.path.exists(fn):
        return send_file(fn, as_attachment=True)
    return "Nenhum ficheiro encontrado", 404


# ------------------------------------------------------------------
# Inicialização – abre a porta série e CSV quando o servidor inicia
# ------------------------------------------------------------------
def start_background():
    # abre CSV (append) e guarda o writer na state global
    fn = f"smc_data_{datetime.now().strftime('%Y%m%d')}.csv"
    csv_file = open(fn, "a", newline="")
    state["csv_writer"] = csv.writer(csv_file)
    state["csv_writer"].writerow(["timestamp", "kW", "evento"])  # header
    # inicia thread Modbus
    t = threading.Thread(target=modbus_reader, daemon=True)
    t.start()


if __name__ == "__main__":
    start_background()
    # debug=False para produção; True ajuda a ver logs no terminal
    app.run(host="0.0.0.0", port=5000, debug=False)