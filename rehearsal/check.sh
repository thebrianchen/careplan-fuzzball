#!/usr/bin/env bash
# Rehearsal boundary checks. Run after `docker compose ... up -d`. Prints one line per check and
# writes rehearsal/check-results.jsonl. Fuzzball-only probes (volumes, tokens, placement) report
# "inconclusive" here by design; they run Friday night on the real cluster.
set -uo pipefail
cd "$(dirname "$0")/.."
C="docker compose -f rehearsal/docker-compose.yml"
out=rehearsal/check-results.jsonl
: > "$out"
row() { printf '%-58s %s\n' "$1" "$2"; printf '{"check": "%s", "result": "%s"}\n' "$1" "$2" >> "$out"; }
code() { $C exec -T "$1" python -c "import sys,urllib.request,urllib.error
req=urllib.request.Request(sys.argv[1],data=(sys.argv[2].encode() if len(sys.argv)>2 and sys.argv[2] else None),headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+sys.argv[3]} if len(sys.argv)>3 and sys.argv[3] else {}),**({'X-Fuzzball-Caller-Identity':sys.argv[4]} if len(sys.argv)>4 else {})})
try: print(urllib.request.urlopen(req,timeout=8).status)
except urllib.error.HTTPError as e: print(e.code)
except Exception as e: print('no-route:'+type(e).__name__)" "${@:2}" 2>/dev/null | tail -1; }
expect() { local name=$1 want=$2 got=$3; [[ "$got" == $want ]] && row "$name" "PASS ($got)" || row "$name" "FAIL (got $got, want $want)"; }

Q='{"tract_id": "17031260300", "need": "food"}'
echo "== box 2 -> box 1 crossing (should work only typed + authenticated)"
expect "planner -> gateway via proxy, svc-private token"        200 "$(code planner http://proxy:8443/gateway/find_resources "$Q" rehearsal-svc-private)"
expect "planner -> gateway via proxy, free text in tract_id"    400 "$(code planner http://proxy:8443/gateway/find_resources '{"tract_id": "Raymond Ellis", "need": "food"}' rehearsal-svc-private)"
echo "== compromised box 1 job (assumed breach)"
expect "attacker -> gateway direct, no identity"                401 "$(code attacker http://gateway:8090/find_resources "$Q")"
expect "attacker -> gateway direct, forged identity"            401 "$(code attacker http://gateway:8090/find_resources "$Q" "" eyJhbGciOiJFUzI1NiIsImtpZCI6ImZvcmdlZCJ9.eyJzdWIiOiJzdmMtcHJpdmF0ZSJ9.Zm9yZ2Vk)"
expect "attacker -> gateway via proxy as svc-public"            403 "$(code attacker http://proxy:8443/gateway/find_resources "$Q" rehearsal-svc-public)"
expect "attacker -> patient card via proxy, no token"           401 "$(code attacker http://proxy:8443/card/D001)"
expect "attacker -> patient card via proxy, svc-public token"   403 "$(code attacker http://proxy:8443/card/D001 "" rehearsal-svc-public)"
expect "attacker -> planner port direct"                        "no-route:*" "$(code attacker http://planner:8080/healthz)"
expect "attacker -> model port direct"                          "no-route:*" "$(code attacker http://llm:8000/v1/models)"
echo "== box 2 egress (should be none)"
expect "planner -> internet"                                    "no-route:*" "$(code planner https://example.com/)"
expect "planner -> box 1 gateway direct (bypassing proxy)"      "no-route:*" "$(code planner http://gateway:8090/healthz)"
echo "== Fuzzball-only probes (run Friday night): 1 mount EHR, 2 placement, 3 mint tokens, 4 objects, 5 secrets"
$C exec -T attacker python fuzzball/probes.py --config correct --only 1,2,3,4,5 2>/dev/null \
  | python3 -c "import sys,json; [print(f\"  probe {d['probe_id']}: {d['result']}\") for d in map(json.loads, sys.stdin)]"
echo "results -> $out"
