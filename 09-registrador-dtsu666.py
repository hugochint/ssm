#!/usr/bin/env python3
"""
Registrador INDEPENDENTE via 2o DTSU666 (Modbus-RTU) - ensaio grid-zero.
Procedimento 06, opcao (b): medicao independente do controle.

Premissa metrologica: este SEGUNDO medidor (com seus proprios TCs, no mesmo
ponto de conexao) e o instrumento do ensaio. O DTSU do controle continua
ligado ao EMS, sem interferencia.

Hardware necessario:
  - 1x DTSU666 (qualquer variante com RS485) + TCs dedicados (ex. 200/5)
    instalados nos mesmos alimentadores do ponto de conexao, mesma
    orientacao (seta no sentido da importacao);
  - Tensao do medidor derivada do mesmo ponto (380 V);
  - 1x conversor RS485-USB (FTDI/CH340, ~R$ 50-120), par trancado A/B,
    resistor 120R se o cabo for longo;
  - Notebook com: pip install pymodbus pyserial

Comunicacao (manual ZTY0.464.1448, defaults de fabrica):
  9600 bps, N.8.1, endereco 01. Para 5 Hz, subir para 19200 (n.8.1) no
  display do medidor ajuda na margem. Nao ha outro mestre neste barramento
  ( so o notebook ), entao nao ha colisao.

COMO DESCOBRIR O REGISTRADOR DA POTENCIA (1x, em campo):
  O manual do DTSU666 documenta os registradores de ENERGIA (ex. 0x101E),
  mas remete o mapa de grandezas instantaneas ao "protocolo detalhado".
  Por isso este script tem MODO SCAN: ele despeja os candidatos como
  float32/IEEE754-ABCD (padrao do DTSU, ver Nota 2 do manual), int16 e
  int32, e a equipe identifica qual acompanha o display do medidor
  ("Combined phase active power", item 12 da tabela de telas).
  Com carga conhecida ligada (ex. 10 kW), o registrador certo e o que
  marca ~10 kW. Depois rode em MODO LOG com --register.

  Exemplo:
    python 09-registrador-dtsu666.py --port COM4 --scan
    python 09-registrador-dtsu666.py --port COM4 --register 0x2014 --ct-ratio 40 --rate 5 --duration 300

ATENCAO CTs: o valor lido e do LADO SECUNDARIO. Multiplique pela relacao
  de TC (--ct-ratio, ex. 200/5 = 40). Confira contra o display do medidor
  antes de cada ensaio. Convencao: positivo = importando (verificar com
  carga ligada e BESS parado; se invertido, anotar e corrigir na analise).
"""

import argparse
import csv
import struct
import sys
import time
from datetime import datetime, timezone


def regs_to_float32_abcd(regs):
    raw = struct.pack(">HH", regs[0], regs[1])
    return struct.unpack(">f", raw)[0]


def regs_to_int32(regs):
    v = (regs[0] << 16) | regs[1]
    return v - 0x100000000 if v >= 0x80000000 else v


def read_regs(client, unit, addr, count, fcode=0x04):
    kw = {"address": addr, "count": count}
    try:
        if fcode == 0x04:
            rr = client.read_input_registers(device_id=unit, **kw)
        else:
            rr = client.read_holding_registers(device_id=unit, **kw)
    except TypeError:
        if fcode == 0x04:
            rr = client.read_input_registers(unit=unit, **kw)
        else:
            rr = client.read_holding_registers(unit=unit, **kw)
    if rr.isError():
        raise IOError("erro Modbus: %s" % rr)
    return rr.registers


def scan(client, unit):
    print("addr(hex)  float32_ABCD      int16     int32")
    for base in range(0x2000, 0x2061, 2):
        for fc in (0x04,):
            try:
                regs = read_regs(client, unit, base, 2, fc)
            except Exception as e:
                print("0x%04X  ERRO %s" % (base, e))
                break
            try:
                f = regs_to_float32_abcd(regs)
            except Exception:
                f = float("nan")
            i16 = regs[0] - 0x10000 if regs[0] >= 0x8000 else regs[0]
            print("0x%04X  %12.3f  %8d  %10d" % (base, f, i16, regs_to_int32(regs)))
    print("\nCompare com o display do medidor (Combined phase active power).")


def make_client(port, baud):
    from pymodbus.client import ModbusSerialClient
    try:
        return ModbusSerialClient(port=port, baudrate=baud, parity="N",
                                  stopbits=1, bytesize=8, timeout=0.5)
    except TypeError:
        from pymodbus.client import ModbusSerialClient as C
        import inspect
        if "framer" in inspect.signature(C.__init__).parameters:
            pass
        return C(port=port, baudrate=baud, parity="N", stopbits=1,
                 bytesize=8, timeout=0.5)


def main():
    ap = argparse.ArgumentParser(description="Registro independente via 2o DTSU666.")
    ap.add_argument("--port", required=True, help="ex. COM4 ou /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--scan", action="store_true", help="despeja registradores p/ achar a potencia")
    ap.add_argument("--register", default=None, help="ex. 0x2014 (descoberto no scan)")
    ap.add_argument("--fcode", type=int, default=4, choices=[3, 4])
    ap.add_argument("--ct-ratio", type=float, default=1.0, help="ex. 40 para TC 200/5")
    ap.add_argument("--rate", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    try:
        import serial  # noqa: F401 (pyserial exigido pelo pymodbus serial)
    except ImportError:
        sys.exit("ERRO: instale com: pip install pymodbus pyserial")

    client = make_client(args.port, args.baud)
    if not client.connect():
        sys.exit("ERRO: nao abriu %s" % args.port)

    if args.scan or args.register is None:
        scan(client, args.unit)
        client.close()
        return

    addr = int(args.register, 16) if isinstance(args.register, str) else int(args.register)
    out = args.out or ("dtsu_independente_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    period = 1.0 / args.rate if args.rate > 0 else 0.2

    # sanity check contra o display
    regs = read_regs(client, args.unit, addr, 2, 0x04 if args.fcode == 4 else 0x03)
    p0 = regs_to_float32_abcd(regs) * args.ct_ratio
    print("Leitura atual: %.2f kW (confira com o display; positivo deve = importando)." % p0)
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
                    regs = read_regs(client, args.unit, addr, 2, 0x04 if args.fcode == 4 else 0x03)
                    p = regs_to_float32_abcd(regs) * args.ct_ratio
                    w.writerow([ts, "%.3f" % p])
                    n += 1
                    if n % int(args.rate * 2) == 0:
                        print("%s  %.2f kW  (%d)" % (ts, p, n))
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
