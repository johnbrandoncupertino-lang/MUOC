# MineBank CC:Tweaked terminal programs

These programs are a prototype client for the feature-flagged MineBank Minecraft API. They do not process payments.

## Files

- `branch_terminal.lua`: authenticates a customer, lists that customer's eligible accounts, asks which account to link, then issues a revocable card credential and writes it to a floppy.
- `balance_reader.lua`: reads the card credential from a floppy and displays the linked account's balance using the read-only API.

## Setup

1. Review and merge the MineBank API pull request only after code review.
2. Apply `extras/minebank_minecraft.sql` to the intended database using the normal reviewed migration process.
3. Configure `MINEBANK_MINECRAFT_ENABLED=true` only in the intended test environment after verifying HTTPS forwarding and the migration.
4. Replace `YOUR-MINEBANK-DOMAIN` in both Lua files with the actual HTTPS origin, for example `https://bank.example.com/api/v1/minecraft`. Keep the `/api/v1/minecraft` suffix.
5. Copy each Lua program to a CC:Tweaked computer. Enable the CC:Tweaked HTTP API in the Minecraft server configuration. The server must permit requests to the MineBank HTTPS hostname.
6. Connect a floppy disk drive to the branch terminal. Use a blank floppy for each new card. The branch terminal refuses to overwrite an existing `minebank.card` file.
7. Run `branch_terminal.lua` to issue a card. Insert that floppy into a computer running `balance_reader.lua` to read the account balance.

## Security and current limitations

- Both scripts require HTTPS URLs; never configure an HTTP endpoint.
- Password and Wallet PIN are prompted at runtime and are not written to the floppy.
- The floppy contains only the revocable card credential and non-sensitive display metadata. Anyone holding a copied credential can use it until MineBank revokes it, so treat the floppy like a payment card.
- If writing the floppy fails after issuance, the branch terminal attempts to revoke the card. If revocation also fails, use the displayed card ID to find and revoke it in the administrative workflow.
- Do not share a floppy, copy its credential, or put card files in public chests.
- Account balances are read from MineBank at request time; the computer does not calculate or store a balance.
- Card payments, transfers, deposits, withdrawals and CashLine settlement are intentionally not implemented.
- The feature is disabled unless `MINEBANK_MINECRAFT_ENABLED=true`. Do not enable it in production until the pull request is reviewed and the deployment has been verified.

## API contract used

- `POST /accounts`: authenticate and list eligible accounts for the signed-in customer.
- `POST /cards`: issue a card credential linked to the selected account; raw credential returned once.
- `GET /account`: read linked account details using `Authorization: Bearer <credential>`.
- `POST /cards/revoke`: revoke a card after MineBank credentials and Wallet PIN are verified.
