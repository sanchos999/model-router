# MegaBrain future integration contract (R8 §27)

MegaBrain is NOT implemented and NOT integrated. This file only fixes the
future boundary.

- MegaBrain will live in its own repository/service:
  /home/sanchos/megabrain/ (future, phase M0+)
- Integration with Model Router is EXCLUSIVELY over the network API:
  MegaBrain is an [OI]-compatible (or control-plane API) CLIENT of Model
  Router, or a separate service Model Router calls over HTTP.
- NO imports between repositories. Model Router must never import
  megabrain modules; MegaBrain must never import gateway/ modules.
- No shared state files; if shared data is ever needed, it goes through
  the control-plane API (versioned, auth-bounded) — never direct DB/file
  access.
- Model Router stays fully functional without MegaBrain present (same
  guarantee as with Hermes).
