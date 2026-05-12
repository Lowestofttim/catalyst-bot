# Sage Setup And Fingerprint Settings Design

## Goal

Make Sage certificate setup and wallet fingerprint switching permanent, visible app features so beta users do not need to edit `.env` by hand.

## Scope

- Improve the existing Sage certificate setup flow instead of replacing it with a larger onboarding wizard.
- Add visible wallet-session controls in Settings while reusing the current wallet picker behavior.
- Persist successful fingerprint switches to `SAGE_FINGERPRINT`.
- Preserve the current safety rule that wallet switching is blocked while the bot is running.

## Architecture

The backend stays in the Sage blueprint because these settings are specific to Sage wallet startup. The existing `/api/sage/setup-certs` endpoint continues to validate and persist certificate paths. A small helper endpoint exposes safe cert-path suggestions for the UI, and a dedicated fingerprint endpoint persists `SAGE_FINGERPRINT` only after validating the selected value.

The desktop bridge mirrors these endpoints so PyWebView mode avoids localhost HTTP. A new bridge method opens a native file picker for `wallet.crt` when available; browser mode keeps the text input/manual paste fallback.

The frontend reuses the existing startup certificate panel and wallet picker modal. Settings > Setup gets a compact Wallet Session section with current fingerprint, change action, and certificate status/edit controls.

## Error Handling And Safety

- Certificate paths must still validate as Sage's `ssl/wallet.crt` with a sibling `wallet.key`.
- The app never reads or returns key contents.
- Fingerprint values must be numeric.
- Fingerprint switching remains blocked while the bot is running or starting.
- Failed cert or fingerprint saves show inline errors and leave the current settings untouched.

## Testing

- Endpoint tests cover cert path suggestions, manual cert persistence, fingerprint validation, bot-running lock, and successful persistence.
- Bridge behavior is kept thin and routed through endpoint tests where possible.
- Frontend changes are manually verified in the app/browser flow after targeted Python tests pass.
