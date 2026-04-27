## Summary

-

## Why this change is needed

-

## Testing

- [ ] Unit tests run
- [ ] Manual app smoke test run, if relevant
- [ ] Wallet-affecting behavior tested with a test wallet only
- [ ] Logs checked for warnings or errors

## Safety Checklist

- [ ] No wallet secrets, certs, private keys, databases, or `.env` files added
- [ ] No raw database access outside `database.py`
- [ ] Prices and amounts use `Decimal`, not `float`
- [ ] User-facing HTML from server data is escaped
- [ ] Trading or wallet behavior has a rollback or recovery path

## Notes for Reviewers

-
