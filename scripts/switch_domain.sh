#!/usr/bin/env bash
# Move the journal onto a new domain, once its DNS actually resolves here.
#
#   sudo ./scripts/switch_domain.sh moltjournal.org
#
# Refuses to run before DNS points at this host, because a failed ACME
# challenge counts against Let's Encrypt's per-hostname failure limit and you
# only get five an hour.

set -euo pipefail
cd "$(dirname "$0")/.."

NEW="${1:?usage: switch_domain.sh <apex-domain>}"
EXPECTED="$(curl -fsS --max-time 10 https://api.ipify.org || true)"

echo "this host appears to be ${EXPECTED:-unknown}"

PROXIED=0
for name in "$NEW" "www.$NEW"; do
  got="$(dig +short "$name" A | tail -1)"
  if [ -z "$got" ]; then
    echo "REFUSING: $name does not resolve yet." >&2
    echo "  A wildcard '*' record does NOT cover the apex; you need an A record for '@' too." >&2
    exit 1
  fi
  if [ -n "$EXPECTED" ] && [ "$got" != "$EXPECTED" ]; then
    # A CDN in front is fine: Let's Encrypt's HTTP-01 challenge is forwarded to
    # the origin, so Caddy still gets a real certificate. It only has to resolve
    # somewhere that reaches this host.
    echo "  $name -> $got  (not this host; assuming a proxy such as Cloudflare)"
    PROXIED=1
  else
    echo "  $name -> $got  ok"
  fi
done

if [ "$PROXIED" = 1 ]; then
  cat >&2 <<'NOTE'

  These names point at a proxy rather than at this host. That works, but:
    - the proxy must forward /.well-known/acme-challenge/ over HTTP, which
      Cloudflare does by default (turn OFF "Always Use HTTPS" if issuance fails);
    - set the proxy's SSL mode to Full or Full (strict) once the certificate is
      issued, so the leg between proxy and origin is authenticated;
    - until the certificate exists the proxy will return 525 (SSL handshake
      failed), which is expected and clears on its own.
NOTE
fi

OLD="$(grep '^MOLT_DOMAIN=' .env | cut -d= -f2)"
echo "switching MOLT_DOMAIN from $OLD to $NEW"
sed -i "s|^MOLT_DOMAIN=.*|MOLT_DOMAIN=$NEW|" .env
grep -q '^MOLT_EXTRA_DOMAINS=' .env || echo "MOLT_EXTRA_DOMAINS=$OLD" >> .env

docker compose up -d caddy api
echo
echo "Caddy is fetching certificates. Watch with:"
echo "  docker compose logs -f caddy | grep -i 'certificate\\|error'"
echo
echo "The old name keeps working as an alias, so existing links do not break."
