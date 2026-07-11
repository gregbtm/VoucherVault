# Setting up Google Wallet export

This is a **one-time setup done by whoever runs the VoucherVault container**
(you, the operator) — not something each person using your instance has to
do. Once you've done this once, everyone who logs into your VoucherVault
gets an "Add to Google Wallet" button on their items automatically, the
same way everyone already gets "Add to Apple Wallet" once you've set up a
Pass Type ID certificate.

The people using your instance (family, friends, whoever you've made
accounts for) don't need a Google Cloud account, don't need to be
"invited" to anything technical, and don't pay anything. They just need:

- An Android phone with the Google Wallet app (comes pre-installed on
  almost all Android phones), **or**
- Any Chromium-based desktop browser (Chrome, Edge) — the save flow opens
  a page that can hand off to their phone.

The one thing to know up front: a brand-new Google Wallet Issuer account
starts in **demo mode**, which only lets you issue passes to Google
accounts you've explicitly listed (see the "Demo mode" section further
down) — for a personal/family NAS deployment like this fork is designed
for, that's usually all you need, and you can skip the "request publishing
access" step entirely.

## What you'll need to get

1. A Google account you're happy to be the "Admin" of this Issuer account
   (your normal Google account is fine).
2. A Google Cloud project (free — no billing required for this).
3. About 15 minutes.

## Step 1 — Create a Google Wallet API Issuer account

1. Go to the [Google Pay & Wallet Console](https://pay.google.com/business/console/)
   and sign in with the Google account from above.
2. Fill in the sign-up form: a public business name for your Issuer account
   (this can be anything — "VoucherVault", your name, your household name —
   it's shown on passes, not verified against a real company), and accept
   the Google Wallet API Additional Terms of Service and the Google privacy
   policy.
3. On the dashboard, find the **Google Wallet API** card and click
   **Create a pass**, then **Build your first pass**, then review and
   accept the Google Wallet API Terms of Service.
4. Once you're through, your **Issuer ID** is shown at the top of the
   dashboard — a long numeric string like `3388000000012345678`. Copy it
   somewhere; this is your `GOOGLE_WALLET_ISSUER_ID`.

## Step 2 — Create a Google Cloud service account + key

VoucherVault authenticates to Google as this service account, not as your
personal Google login.

1. Go to the [Google Cloud console](https://console.cloud.google.com/) and
   select or create a project (any project name is fine).
2. Open the [Google Wallet API page](https://console.cloud.google.com/apis/library/walletobjects.googleapis.com)
   for that project and click **Enable**.
3. Go to [**Create service account**](https://console.cloud.google.com/iam-admin/serviceaccounts/create),
   give it any name (e.g. `vouchervault-wallet`), and click through the
   remaining steps with the defaults — this app doesn't need any Cloud IAM
   roles granted, since all the real permissions live in the Wallet Issuer
   account from Step 1.
4. Open the new service account, go to its **Keys** tab, click
   **Add key → Create new key**, choose **JSON**, and click **Create**.
   A `.json` file downloads — this is your
   `GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH` file. Keep it secret; it's a
   credential, not just an ID.
5. Note the service account's **email address** (shown on its details page,
   looks like `vouchervault-wallet@your-project.iam.gserviceaccount.com`)
   — you need it for the next step.

## Step 3 — Authorize the service account on your Issuer account

Back in the [Google Pay & Wallet Console](https://pay.google.com/business/console/):

1. Open **Users** in the left-hand navigation.
2. Click **Invite a user**, paste in the service account's email address
   from Step 2.5, set its access level to **Developer**, and confirm.

This is what lets the JSON key from Step 2 actually create passes under
your Issuer ID — without this step, VoucherVault's requests will be
rejected even with a valid key.

## Demo mode — the part that matters for personal use

Every new Issuer account starts in demo mode: you can create and issue
passes, but **only** to Google accounts that are either the Admin/Developer
users on the Issuer account (i.e. you) or accounts you've explicitly added
as testers. Anyone else tapping "Add to Google Wallet" will get an error.

For a household/family deployment, this is usually fine as a permanent
state — add each family member's Google account under **Users** in the
console (same place as Step 3) with at least tester/developer-level access,
and everyone who needs to use the button can. There's no review, no
waiting, and no request to submit.

If you'd rather any Google account work without being individually listed
(e.g. you're sharing VoucherVault more widely than your household), you
can request full publishing access instead: complete your Business Profile
in the console, create at least one pass class (VoucherVault does this
automatically the first time someone successfully saves a pass), then go
to **Google Wallet API → Request publishing access** in the console.
Google's Wallet team reviews the request manually before approving it, so
budget a few business days — this isn't something VoucherVault or this
guide can speed up.

## Step 4 — Configure VoucherVault

1. Mount the JSON key file into the container. Using the bundled Docker
   Compose files as an example, drop it at
   `./volume-data/google-wallet/key.json` next to your existing
   `volume-data/database`, then in `docker/docker-compose-sqlite-build.yml`
   (or the `-full-build.yml` variant) uncomment:

   ```yaml
   volumes:
     - ./volume-data/google-wallet:/opt/app/google-wallet:ro
   ```

2. Set the environment variables (see `docker/env.example`):

   ```
   GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH=/opt/app/google-wallet/key.json
   GOOGLE_WALLET_ISSUER_ID=3388000000012345678
   ```

   `GOOGLE_WALLET_CLASS_ID` is optional — leave it unset and VoucherVault
   will use `<your issuer id>.vouchervault_generic` automatically.

3. Redeploy/recreate the container so it picks up the new environment
   variables and volume mount.

4. Open any item's detail page on an Android phone or a Chromium desktop
   browser — you should now see an **Add to Google Wallet** button (Apple
   devices continue to see **Add to Apple Wallet** instead, if you've set
   that up too; see the Apple Wallet variables in `docker/env.example`).

## Troubleshooting

- **Button doesn't appear at all**: both `GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH`
  and `GOOGLE_WALLET_ISSUER_ID` must be set, and the key file must actually
  be readable inside the container at that path — check the volume mount.
- **Button appears but tapping it shows a Google error page**: almost
  always demo mode (see above) — the account you're signed into on that
  device hasn't been added as a user/tester on the Issuer account, or the
  service account wasn't invited as a Developer (Step 3).
- **"Invalid JWT" or similar from Google**: the service account key file is
  malformed, the wrong file, or the service account was deleted/recreated
  after the key was issued — regenerate the key (Step 2.4) and redeploy.

## One more thing: this is per-deployment, not per-user

To directly answer the question this guide exists to answer: no, nobody
using your VoucherVault instance needs to do any of the above themselves.
This is a single Issuer account and a single service account key,
configured once at the container level, shared by every user of that
container — exactly like the Apple Wallet certificate setup. Individual
users just need a Google account (Android) or a Chromium browser, same as
they'd need to have Apple Wallet on an iPhone.
