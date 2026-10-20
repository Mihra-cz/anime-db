# Změny instalačních podkladů

## 2026-10-20 — N2, pouze lokální příprava

- Připraven Nexus Compose s izolovanými pracovními DB A/B, persistentním
  artworkem, localhost porty, read-only NAS a kontrolou před Uvicornem.
- Připraven manifest podle skutečné šablony Cookbook v1.0 a návod umístění
  souborů, přípravy dat, validace a bezpečného zastavení při chybě.
- Stav je `planned / not-deployed`. Na Nexusu se nic neinstalovalo;
  DB, NAS, služby ani zálohování se neměnily. ARM64 build/runtime a obnova
  zůstávají neověřené.
