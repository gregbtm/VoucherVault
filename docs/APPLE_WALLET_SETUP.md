# Setting up Apple Wallet export

This is a **one-time setup done by whoever runs the VoucherVault container**
(you, the operator) — not something each person using your instance has to
do. Once you've done this once, everyone who logs into your VoucherVault
gets an "Add to Apple Wallet" button on their items automatically, the same
way everyone already gets "Add to Google Wallet" once you've set that up.

The people using your instance don't need an Apple Developer account,
don't need to be "invited" to anything, and don't pay anything. They just
need an iPhone/iPad, or a Mac with Safari.

Note: this is only about **export** (the "Add to Apple Wallet" button).
**Importing** an existing `.pkpass` file someone sends you — the upload box
on the create-item page — works with zero configuration, on any device;
it's plain file parsing and doesn't need any of the certificates below.

## What you'll need to get

1. An [Apple Developer Program](https://developer.apple.com/programs/)
   membership — **$99/year**, paid by you as the operator, not by anyone
   using your instance. There's no free tier for this specific capability;
   Apple requires a paid membership to issue Pass Type ID certificates.
2. Access to a Mac (even briefly, e.g. a friend's, or a cloud Mac) — the
   certificate request/export step below uses macOS's Keychain Access app.
   You only need it for this one-time setup, not on an ongoing basis.
3. About 20 minutes, plus Apple's account approval time if you're enrolling
   fresh (usually same-day, occasionally up to 48 hours).

## Step 1 — Register a Pass Type ID

1. Sign in to the [Apple Developer portal](https://developer.apple.com/account/resources/identifiers/list/passTypeId).
2. Under **Identifiers**, click **+**, choose **Pass Type IDs**, and
   continue.
3. Give it a description (e.g. "VoucherVault") and an identifier in reverse-DNS
   form, e.g. `pass.com.yourdomain.vouchervault` (it doesn't need to match a
   real domain you own — any reverse-DNS-style string is accepted). This
   full string is your `PKPASS_PASS_TYPE_ID`.
4. Register it.

## Step 2 — Create the certificate (on a Mac)

1. On the Mac, open **Keychain Access → Certificate Assistant → Request a
   Certificate from a Certificate Authority**. Fill in your email, leave
   "CA Email Address" blank, select **Saved to disk**, and save a
   `.certSigningRequest` file (a CSR).
2. Back in the [Developer portal](https://developer.apple.com/account/resources/certificates/list),
   under **Certificates**, click **+**, choose **Pass Type ID Certificate**,
   select the Pass Type ID from Step 1, and upload the CSR from step 1.
3. Download the resulting `.cer` file and double-click it to import it into
   Keychain Access.
4. In Keychain Access, find the certificate (under **My Certificates** —
   it'll be paired with the private key from the CSR), right-click it, and
   choose **Export**. Save it as a `.p12` file, optionally setting a
   password — this is your `PKPASS_CERT_PATH` file, and the password (if
   any) is `PKPASS_CERT_PASSWORD`.

## Step 3 — Get Apple's WWDR intermediate certificate

1. Download the current **Worldwide Developer Relations - G4** certificate
   from [Apple's certificate authority page](https://www.apple.com/certificateauthority/).
2. VoucherVault needs it in PEM format, not the `.cer` Apple ships. Convert
   it once, on any machine with OpenSSL:
   ```
   openssl x509 -inform der -in AppleWWDRCAG4.cer -out AppleWWDRCAG4.pem
   ```
   The resulting `.pem` file is your `PKPASS_WWDR_CERT_PATH`.

## Step 4 — Note your Team ID

Your **Team ID** is shown on the [Membership page](https://developer.apple.com/account/#/membership)
of the Developer portal — a 10-character alphanumeric string. This is your
`PKPASS_TEAM_ID`.

## Step 5 — Configure VoucherVault

1. Mount both certificate files into the container. Using the bundled
   Docker Compose files as an example, drop them at
   `./volume-data/pkpass/pass-cert.p12` and
   `./volume-data/pkpass/AppleWWDRCAG4.pem` next to your existing
   `volume-data/database`, then in `docker/docker-compose-sqlite-build.yml`
   (or the `-full-build.yml` variant) uncomment:

   ```yaml
   volumes:
     - ./volume-data/pkpass:/opt/app/pkpass:ro
   ```

2. Point VoucherVault at the files and your IDs. **The easiest way**: log
   in as a superuser, open **Site Settings**
   (`/admin-tools/site-settings/`, linked from the sidebar), and fill in
   the five fields under the **Apple Wallet (.pkpass)** section
   (Certificate path, Certificate password, WWDR certificate path, Team ID,
   Pass Type ID). This takes effect immediately — no redeploy, no `.env`
   file, no restart.

   Alternatively, set the environment variables (see `docker/env.example`),
   which is what Site Settings falls back to until you (or a fresh install)
   set it there instead:

   ```
   PKPASS_CERT_PATH=/opt/app/pkpass/pass-cert.p12
   PKPASS_CERT_PASSWORD=
   PKPASS_WWDR_CERT_PATH=/opt/app/pkpass/AppleWWDRCAG4.pem
   PKPASS_TEAM_ID=ABCDE12345
   PKPASS_PASS_TYPE_ID=pass.com.yourdomain.vouchervault
   ```

3. If you set the values via environment variables rather than Site
   Settings, redeploy/recreate the container so it picks up the new
   environment variables and volume mount.

4. Open any item's detail page on an iPhone/iPad or a Mac in Safari — you
   should now see an **Add to Apple Wallet** button (Android/Chromium
   devices continue to see **Add to Google Wallet** instead, if you've set
   that up too).

## Troubleshooting

- **Button doesn't appear at all**: check the Site Settings page — it now
  shows a "Ready"/"Not ready" badge next to this section confirming
  whether the certificate file was actually found at the configured path
  inside the container (not just whether the field is filled in).
- **"Cannot Add Pass" or a signature error on the device**: almost always
  a mismatched WWDR certificate (Apple rotates these periodically — make
  sure you downloaded the current G4 one) or a Pass Type ID mismatch
  between the certificate (Step 2) and `PKPASS_PASS_TYPE_ID` (they must be
  for the exact same identifier).
- **Certificate password errors**: if you set a password while exporting
  the `.p12` in Step 2.4, it must match `PKPASS_CERT_PASSWORD` exactly —
  re-export without a password if you want to leave that field blank.

## One more thing: this is per-deployment, not per-user

No one using your VoucherVault instance needs to do any of the above
themselves. This is a single certificate and Pass Type ID, configured once
at the container level, shared by every user of that container — exactly
like the Google Wallet service account setup. Individual users just need
an Apple device, the same way they'd need Android or a Chromium browser
for Google Wallet.
