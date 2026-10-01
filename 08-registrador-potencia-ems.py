#!/usr/bin/env python3
"""
Registrador de potencia no ponto de conexao via EMS (Modbus-TCP).
Uso no ensaio de tempo de resposta do grid-zero (procedimento 06).

Le o ponto "Smart Grid Meter - Active power" do EMS CPS C 1000:
  - Funcao: 0x04 (Read Input Registers)
  - Endereco: 0x0106 (decimal 262), 2 registradores, int32 big-endian
  - Escala: x0.1 kW  (valor 123 = 12.3 kW)
  - Convencao de sinal: positivo = IMPORTACAO da rede (confirmar em campo
    com uma carga ligada e sem geracao; se aparecer negativo, inverter o
    sinal na analise e anotar no relatorio).

Requisitos no notebook:
  pip install pymodbus
  Cabo Ethernet notebook <-> EMS (mesma rede). Porta Modbus-TCP padrao: 502.
  Verificar o unit id / endereco IP do EMS no display ou no manual.

Exemplos:
  python 08-registrador-potencia-ems.py --host 192.168.1.10 --rate 5 --duration 300
  python 08-registrador-potencia-ems.py --host 192.168.1.10 --rate 5 --duration 0   (ate Ctrl+C)

Saida: CSV com timestamp ISO, potencia kW e flag de evento (o operador digita
o instante do degrau em outro terminal ou anota o horario para sincronizar).

Alternativa sem Python: mbpoll / QModMaster lendo 0x0106 (2 regs, int32BE).
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone

REG_ADDR = 0x0106
REG_COUNT = 2
SCALE = 0.1


def read_power_kw(client, unit):
    try:
        rr = client.read_input_registers(address=REG_ADDR, count=REG_COUNT, device_id=unit)
    except TypeError:
        # pymodbus <3 usa 'unit' em vez de 'device_id'
        rr = client.read_input_registers(address=REG_ADDR, count=REG_COUNT, unit=unit)
    if rr.isError():
        raise IOError("erro Modbus: %s" % rr)
    regs = rr.registers
    raw = (regs[0] << 16) | regs[1]
    if raw >= 0x80000000:
        raw -= 0x100000000
    return raw * SCALE


def main():
    ap = argparse.ArgumentParser(description="Registra potencia do medidor da rede via EMS (Modbus-TCP).")
    ap.add_argument("--host", required=True, help="IP do EMS")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=1, help="unit id Modbus (verificar no EMS)")
    ap.add_argument("--rate", type=float, default=5.0, help="leituras por segundo (minimo 5 p/ provar 2 s)")
    ap.add_argument("--duration", type=float, default=0, help="duracao em s (0 = ate Ctrl+C)")
    ap.add_argument("--out", default=None, help="arquivo CSV de saida")
    args = ap.parse_args()

    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError:
        sys.exit("ERRO: instale com: pip install pymodbus")

    out = args.out or ("potencia_rede_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    period = 1.0 / args.rate if args.rate > 0 else 0.2

    client = ModbusTcpClient(args.host, port=args.port)
    if not client.connect():
        sys.exit("ERRO: nao conectou em %s:%s" % (args.host, args.port))

    # leitura de sanity check
    try:
        p0 = read_power_kw(client, args.unit)
    except Exception as e:
        sys.exit("ERRO na primeira leitura (confira IP/unit id): %s" % e)
    print("Conectado. Primeira leitura: %.1f kW (positivo deve = importando)." % p0)
    print("Registrando a %.1f Hz em %s ... Ctrl+C para parar." % (args.rate, out))

    t_end = (time.monotonic() + args.duration) if args.duration > 0 else None
    n = 0
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_iso", "potencia_kw"])
        try:
            while True:
                t0 = time.monotonic()
                ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
                try:
                    p = read_power_kw(client, args.unit)
                    w.writerow([ts, "%.3f" % p])
                    n += 1
                    if n % int(args.rate * 2) == 0:
                        print("%s  %.2f kW  (%d leituras)" % (ts, p, n))
                except Exception as e:
                    w.writerow([ts, "ERRO: %s" % e])
                if t_end is not None and time.monotonic() >= t_end:
                    break
                dt = period - (time.monotonic() - t0)
                if dt > 0:
                    time.sleep(dt)
        except KeyboardInterrupt:
            pass
    client.close()
    print("Fim. %d leituras em %s" % (n, out))


if __name__ == "__main__":
    main()
