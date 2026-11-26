#!/bin/bash
# Check that the service is reachable from outside at its public address, and that the API and
# the console ask for a key and a token. Run on the Mac mini after adding the public hostname in Cloudflare.
#   ./scripts/check_public.sh ocr.getcdn.link
set -uo pipefail
HOST="${1:-}"; [ -n "$HOST" ] || { echo "Usage: $0 <public hostname>, for example: $0 ocr.getcdn.link"; exit 1; }
HOST="${HOST#https://}"; HOST="${HOST%%/*}"
PORT="${OCR_PORT:-8090}"
ok=1
say() { printf '%s %s\n' "$1" "$2"; }

if curl -fs -m 5 "http://localhost:$PORT/health" >/dev/null; then say "✓" "The service answers on this Mac (port $PORT)."
else say "✗" "The service does not answer on this Mac at port $PORT. Start it first: ./scripts/install_service.sh"; exit 1; fi

code="$(curl -s -m 20 -o /tmp/ocr-public-health.$$ -w '%{http_code}' "https://$HOST/health" || echo 000)"
if [ "$code" = 200 ] && grep -q '"engine"' /tmp/ocr-public-health.$$; then say "✓" "https://$HOST/health answers from outside."
else
  ok=0; say "✗" "https://$HOST/health answered $code, not this service."
  echo "    In Cloudflare Zero Trust > Networks > Tunnels > the tunnel that runs on this Mac > Public Hostname,"
  echo "    add: subdomain '${HOST%%.*}', domain '${HOST#*.}', service HTTP, URL localhost:$PORT. Then run this again."
fi
rm -f /tmp/ocr-public-health.$$

code="$(curl -s -m 20 -o /dev/null -w '%{http_code}' "https://$HOST/admin" || echo 000)"
api="$(curl -s -m 20 -o /dev/null -w '%{http_code}' "https://$HOST/admin/api/clients" || echo 000)"
case "$code/$api" in
  200/401) say "✓" "The console is at https://$HOST/admin and asks for a token." ;;
  404/*|403/*|302/*) say "✓" "The console is not reachable from outside (https://$HOST/admin answers $code)." ;;
  200/200) ok=0; say "✗" "The console's data is readable from outside without a token. Stop the service and tell whoever maintains it." ;;
  *) say "•" "https://$HOST/admin answered $code, its API $api." ;;
esac

code="$(curl -s -m 20 -o /dev/null -w '%{http_code}' "https://$HOST/openapi.json" || echo 000)"
if [ "$code" = 200 ]; then say "•" "The API documentation (https://$HOST/docs) is open to anyone. To close it, add a Cloudflare rule as in the README."
else say "✓" "The API documentation is not open from outside (answers $code)."; fi

code="$(curl -s -m 20 -o /dev/null -w '%{http_code}' -X POST "https://$HOST/v1/ocr" || echo 000)"
if [ "$code" = 401 ]; then say "✓" "The API asks for a key (a call without one answers 401)."
elif [ "$ok" = 1 ]; then ok=0; say "✗" "A call without a key answered $code; 401 was expected."; fi

[ "$ok" = 1 ] && echo "Ready: give an app the address https://$HOST and an API key from the console (API clients > New)." || exit 1
