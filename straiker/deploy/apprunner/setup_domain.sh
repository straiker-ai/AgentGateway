#!/usr/bin/env bash
# Associate your gateway host with the App Runner service and create ALL required
# Route53 records automatically (dev.straiker.ai is a hosted zone in this account):
#   - the ACM certificate-validation CNAMEs App Runner returns
#   - the CNAME pointing your gateway host at the App Runner DNSTarget
# Idempotent (UPSERT). Requires the service to be RUNNING.
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
DOMAIN="${STRAIKER_GATEWAY_DOMAIN:?set STRAIKER_GATEWAY_DOMAIN}"
ZONE_ID="${ZONE_ID:?set ZONE_ID (your Route53 hosted zone id)}"
SVC="straiker-agentgateway"
export AWS_PAGER=""

ARN=$(aws apprunner list-services --region "$REGION" \
        --query "ServiceSummaryList[?ServiceName=='$SVC'].ServiceArn" --output text)
[ -n "$ARN" ] || { echo "service $SVC not found"; exit 1; }

STATUS=$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" --query Service.Status --output text)
echo "service status: $STATUS"
[ "$STATUS" = "RUNNING" ] || { echo "service must be RUNNING before associating a domain"; exit 1; }

echo "== associate custom domain (idempotent) =="
aws apprunner associate-custom-domain --service-arn "$ARN" --domain-name "$DOMAIN" \
  --region "$REGION" >/dev/null 2>&1 || echo "  (already associated)"

echo "== wait for validation records to appear =="
for i in $(seq 1 30); do
  JSON=$(aws apprunner describe-custom-domains --service-arn "$ARN" --region "$REGION" --output json)
  N=$(echo "$JSON" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(sum(len(c.get("CertificateValidationRecords") or []) for c in d.get("CustomDomains",[])))')
  [ "$N" -gt 0 ] && break
  sleep 5
done

TARGET=$(echo "$JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["DNSTarget"])')
echo "  DNSTarget: $TARGET  (validation records: $N)"

echo "== build Route53 change batch =="
# NB: pass the JSON via a FILE, not a pipe — a heredoc script replaces stdin, so
# json.load(sys.stdin) would read the (already consumed) script instead of the data.
JSON_FILE="$(mktemp)"; printf '%s' "$JSON" > "$JSON_FILE"
BATCH=$(python3 - "$DOMAIN" "$TARGET" "$JSON_FILE" <<'PY'
import json,sys
domain,target=sys.argv[1],sys.argv[2]
d=json.load(open(sys.argv[3]))
ch=[{"Action":"UPSERT","ResourceRecordSet":{"Name":domain,"Type":"CNAME","TTL":300,
     "ResourceRecords":[{"Value":target}]}}]
seen=set()
for c in d.get("CustomDomains",[]):
    for r in (c.get("CertificateValidationRecords") or []):
        k=(r["Name"],r["Value"])
        if k in seen: continue
        seen.add(k)
        ch.append({"Action":"UPSERT","ResourceRecordSet":{"Name":r["Name"],"Type":r["Type"],"TTL":300,
                   "ResourceRecords":[{"Value":r["Value"]}]}})
print(json.dumps({"Comment":"App Runner agentgateway","Changes":ch}))
PY
)
echo "$BATCH" | python3 -c 'import json,sys;[print("   ",c["ResourceRecordSet"]["Type"],c["ResourceRecordSet"]["Name"],"->",c["ResourceRecordSet"]["ResourceRecords"][0]["Value"][:60]) for c in json.load(sys.stdin)["Changes"]]'

echo "== apply to Route53 zone $ZONE_ID =="
CID=$(aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" \
        --change-batch "$BATCH" --query ChangeInfo.Id --output text)
echo "  change: $CID"
aws route53 wait resource-record-sets-changed --id "$CID" && echo "  DNS propagated"

echo "== waiting for App Runner to validate the certificate (can take several minutes) =="
for i in $(seq 1 60); do
  ST=$(aws apprunner describe-custom-domains --service-arn "$ARN" --region "$REGION" \
        --query "CustomDomains[?DomainName=='$DOMAIN'].Status" --output text)
  echo "  status: $ST"
  [ "$(echo "$ST" | tr "[:upper:]" "[:lower:]")" = "active" ] && { echo "DONE -> https://$DOMAIN"; exit 0; }
  sleep 20
done
echo "still pending; re-run this script or check the console."
