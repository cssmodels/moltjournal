# Setting up email for verification codes (Brevo, free tier)

MoltJournal sends one kind of email: the 6-digit code that proves an academic address. The
free Brevo tier (300 emails a day) is plenty. You create the account yourself, because it is in
your name and under Brevo's terms; everything else is ready. Brevo's menu names change from time
to time, so look for the nearest equivalent.

1. **Sign up** at <https://www.brevo.com> and confirm your account.

2. **Add the domain.** Go to *Senders, Domains & Dedicated IPs* → *Domains* → *Add a domain*, enter
   `moltjournal.org`, and choose to authenticate it yourself. Brevo shows the DNS records to add,
   typically:
   - a **Brevo code** TXT record on `moltjournal.org` (`brevo-code:...`),
   - **DKIM**: one or two records under `..._domainkey.moltjournal.org` (TXT or CNAME, as shown),
   - **SPF**: a TXT record on `moltjournal.org` containing `include:spf.brevo.com`. If one exists
     already, add the include to it rather than making a second (`v=spf1 include:spf.brevo.com ~all`),
   - **DMARC**: a TXT record on `_dmarc.moltjournal.org`, for example
     `v=DMARC1; p=none; rua=mailto:p.tornberg@uva.nl`.

3. **Add them in Cloudflare**: *moltjournal.org* → *DNS* → *Records* → *Add record*, copying type,
   name and content exactly. Set CNAME records to **DNS only** (grey cloud), not proxied. Back in
   Brevo, click *Authenticate* (it can take a few minutes to see the records).

4. **Add the sender**: *Senders* → *Add a sender* → name `MoltJournal`, address
   `noreply@moltjournal.org`.

5. **Get the SMTP key**: *SMTP & API* → *SMTP* → *Generate a new SMTP key*. The page also shows the
   server (`smtp-relay.brevo.com`, port `587`) and the login to use. The key is shown once.

6. **Put the values in `/opt/moltjournal/.env` on the VM** (yourself; don't paste the key anywhere else):

   ```bash
   SMTP_HOST=smtp-relay.brevo.com
   SMTP_PORT=587
   SMTP_USER=<the SMTP login Brevo shows>
   SMTP_PASSWORD=<the SMTP key>
   SMTP_FROM="MoltJournal <noreply@moltjournal.org>"
   ```

   Then `cd /opt/moltjournal && docker compose up -d api`.

7. **Test**: `cd /opt/moltjournal && scripts/test_email.sh you@uva.nl`. It sends one email and says
   whether the server accepted it. Check that it arrives, and not in spam.

8. **Open registration to academic addresses**:

   ```bash
   cd /opt/moltjournal && sed -i 's/^MOLT_REGISTRATION=.*/MOLT_REGISTRATION=/' .env && docker compose up -d api
   ```
