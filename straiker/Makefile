# Straiker x agentgateway — one-command gates. `make test` is the offline suite (no creds).
VENV=.venv/bin
.PHONY: test qa parity latency compose promote deploy domain clean
test qa:            ## offline behavioural QA + relay byte-identity (no network)
	$(VENV)/python spec/qa.py
parity:             ## central-parse recall vs the the recorded fixtures (needs STRAIKER_AGENTGATEWAY_KEY)
	$(VENV)/python spec/parity_check.py
latency:            ## detect round-trip p50/p95 on real bodies (needs key)
	$(VENV)/python spec/latency.py
compose:            ## build + run locally (agentgateway + sidecar in one container)
	docker build -f gateway/Dockerfile -t straiker-agentgateway:local . && \
	docker run --rm -p 3000:3000 -e STRAIKER_AGENTGATEWAY_KEY -e ANTHROPIC_API_KEY=$$ANTHROPIC_KEY -e STRAIKER_MODE=block straiker-agentgateway:local
promote:            ## qa -> parity -> validate-against-pinned -> build -> deploy -> live_qa
	./promote.sh
deploy:             ## build + push + App Runner (your gateway host)
	./deploy/apprunner/deploy.sh
domain:             ## associate your gateway host + Route53
	./deploy/apprunner/setup_domain.sh
clean:
	rm -rf spec/out lab/out **/__pycache__
