# `src/sva/` — bound assertion contracts

`cxp_sva.sv` holds properties the RTL guarantees on its own, whatever its
inputs do, so they hold in every bench. Each checker is `bind`-ed into the
module it watches (arbiter, inserter, framer, short-packet sources, stream
FIFO, the downlink IDLE rule, …); the header of `cxp_sva.sv` lists them.

| Checker | Bound into | Guarantees |
|---|---|---|
| `cxp_arbiter_sva` | `cxp_tx_arbiter` | one long-packet source taken per word |
| `cxp_inserter_sva` | `cxp_tx_inserter` | a two-word packet is never split (a trigger leader is followed by its Delay word); an offered I/O acknowledgment is on its way within 3 words; at most 99 words between IDLEs |
| `cxp_tx_owner_sva` | `cxp_tx_domain` | the long packet that owns the arbiter offers a word every cycle |
| `cxp_framer_sva` | `cxp_tx_pkt_framer` | SOP / EOP only on valid words; no second SOP inside a packet; header, CRC and EOP words held while not accepted |
| `cxp_short_pkt_sva` | `cxp_tx_short_pkt` | a word that is not accepted is held |
| `cxp_idle_rule_sva` | `cxp_tx_domain` | §8.2.5.1: an IDLE word at least every 100 downlink words |
| `cxp_cdc_stream_fifo_sva` | `cxp_cdc_stream_fifo` | never more than `p_DEPTH` words; no pop when empty (while both sides are up) |
| `cxp_link_mon_sva` | `cxp_rx_link_mon` | uplink framing: bounded bad words before a resync; every resync flushes |
| `cxp_rxlong_sva` | `cxp_rx_packet_parser` | every SOP closed by one EOP before the next SOP; `err` only inside a packet |
| `cxp_ctrl_exec_sva` | `cxp_ctrl_bus_master` | a command (and a drained access) ends within the bus timeout; at most one Wait per command, none after a 0xFF; the response holds until taken; a waiting command starts once the executor is free |
| `cxp_conn_reset_sva` | `cxp_ctrl_bootstrap_regs` | the ConnectionReset bit clears within its timeout |
| `cxp_reset_order_sva` | `cxp_cdc_reset` | domain resets released rx, then tx, then app, and asserted together |

* Compile after the IP: `-F src/rtl/cxp_ip.f -F src/sva/cxp_sva.f`
  (`src/rtl/cxp_ip.mk` adds them as `SVA_SOURCES`).
* Build with assertions on (Verilator `--assert`); `src/verif/common/cocotb_sim.mk`
  does this for every unit bench and PyUVM run.
* Under Questa, `+define+CXP_SVA_FATAL` makes a failing property `$fatal`
  so it gates the run.
* `make lint` lints the IP with these checkers bound.
