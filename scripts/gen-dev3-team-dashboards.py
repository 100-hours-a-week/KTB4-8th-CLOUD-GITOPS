"""Generate the dev3 team dashboards provisioned by Grafana's ConfigMap sidecar."""
import json
from pathlib import Path


OUTPUT = Path(__file__).resolve().parents[1] / "platform/monitoring/dev3/team-dashboards.yaml"
PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}
CLOUDWATCH = {"type": "cloudwatch", "uid": "cloudwatch"}
BACKEND_NAMESPACES = ("account", "ingest", "content", "recommend")
AI_SELECTOR = 'namespace="ai",app_kubernetes_io_name="ai-api"'
BACKEND_SELECTOR = f'namespace=~"{"|".join(BACKEND_NAMESPACES)}"'
WEB_ALB_DIMENSIONS = {
    "LoadBalancer": ["app/k8s-gateways-keepgode-f910baee5a/66923bc35dd68ff0"],
    "TargetGroup": ["targetgroup/k8s-frontend-web-ac81220177/2eb970a09dd2db6a"],
}
TEAM_NAMES = {"ai": "AI", "backend": "백엔드", "frontend": "프론트"}


def prom(expr, legend=""):
    return {"expr": expr, "legendFormat": legend, "range": True, "instant": False}


def web_metric(name, statistic):
    return {
        "queryMode": "Metrics",
        "region": "ap-northeast-2",
        "namespace": "AWS/ApplicationELB",
        "metricName": name,
        "dimensions": WEB_ALB_DIMENSIONS,
        "statistic": statistic,
        "period": "60",
        "matchExact": True,
        "metricQueryType": 0,
        "metricEditorMode": 0,
    }


def panel(title, unit, *queries, datasource=PROMETHEUS, description=""):
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": datasource,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"tooltip": {"mode": "multi"}},
        "targets": [
            {**query, "refId": chr(65 + i), "datasource": datasource}
            for i, query in enumerate(queries)
        ],
    }


def log_panel(namespaces):
    return {
        "type": "logs",
        "title": "전체 앱 로그",
        "description": "시간 범위에 포함된 모든 Pod와 로그 레벨을 조회합니다. CloudWatch 보존 기간은 7일입니다.",
        "datasource": CLOUDWATCH,
        "options": {
            "showTime": True,
            "enableLogDetails": True,
            "dedupStrategy": "none",
            "sortOrder": "Descending",
        },
        "targets": [{
            "refId": "A",
            "datasource": CLOUDWATCH,
            "queryMode": "Logs",
            "region": "ap-northeast-2",
            "logGroupNames": [f"/aws/eks/keepgo-dev3/{ns}" for ns in namespaces],
            "expression": "fields @timestamp, kubernetes.namespace_name, kubernetes.pod_name, @message | sort @timestamp desc",
        }],
    }


def ready_pods(selector):
    return panel("준비된 Pod", "short", prom(
        f'kube_deployment_status_replicas_available{{{selector}}}', "{{deployment}}",
    ))


def dashboard(team, metrics, namespaces):
    team_links = [
        {"title": name, "type": "link", "url": f"/grafana/d/keepgo-dev3-{key}", "keepTime": True}
        for key, name in TEAM_NAMES.items() if key != team
    ]
    team_links.append({"title": "AWS 자원", "type": "link", "url": "/grafana/d/keepgo-v3-aws", "keepTime": True})
    for index, metric in enumerate(metrics):
        metric.update(id=index + 1, gridPos={"x": (index % 2) * 12, "y": (index // 2) * 8, "w": 12, "h": 8})
    logs = log_panel(namespaces)
    logs.update(id=len(metrics) + 1, gridPos={"x": 0, "y": ((len(metrics) + 1) // 2) * 8, "w": 24, "h": 15})
    return {
        "uid": f"keepgo-dev3-{team}",
        "title": f"KeepGo V3 / {TEAM_NAMES[team]}",
        "tags": ["keepgo", "dev3", team],
        "timezone": "browser",
        "schemaVersion": 39,
        "editable": False,
        "time": {"from": "now-6h", "to": "now"},
        "links": team_links,
        "panels": [*metrics, logs],
    }


ai = dashboard("ai", [
    panel("AI 요청 수", "reqps", prom(f'sum(rate(keepgo_http_requests_total{{{AI_SELECTOR}}}[5m]))')),
    panel("AI p95 응답 시간", "s", prom(
        f'histogram_quantile(0.95,sum by(le)(rate(keepgo_http_request_duration_seconds_bucket{{{AI_SELECTOR}}}[5m])))',
    )),
    panel("AI 평균 동시 처리 추정", "short", prom(
        f'sum(rate(keepgo_http_request_duration_seconds_sum{{{AI_SELECTOR}}}[2m]))',
    ), description="완료된 요청의 처리 시간으로 추정한 최근 2분 평균입니다."),
    ready_pods('namespace="ai",deployment="ai-api"'),
], ("ai",))

backend_requests = f'sum by(app_kubernetes_io_name)(rate(http_server_requests_seconds_count{{{BACKEND_SELECTOR}}}[5m]))'
backend_errors = f'sum by(app_kubernetes_io_name)(rate(http_server_requests_seconds_count{{{BACKEND_SELECTOR},status=~"5.."}}[5m]))'
service_legend = "{{app_kubernetes_io_name}}"
backend = dashboard("backend", [
    panel("API 요청 수", "reqps", prom(backend_requests, service_legend)),
    panel("API 5xx 응답 수", "reqps", prom(f'{backend_errors} or (0 * {backend_requests})', service_legend)),
    panel("API p95 응답 시간", "s", prom(
        f'histogram_quantile(0.95,sum by(le,app_kubernetes_io_name)(rate(http_server_requests_seconds_bucket{{{BACKEND_SELECTOR}}}[5m])))',
        service_legend,
    )),
    ready_pods(BACKEND_SELECTOR),
], BACKEND_NAMESPACES)

web_selector = 'namespace="frontend",pod=~"web-.*",container="app"'
frontend = dashboard("frontend", [
    panel("웹 요청 수(1분)", "short", web_metric("RequestCount", "Sum"), datasource=CLOUDWATCH),
    panel("웹 응답 시간 p95", "s", web_metric("TargetResponseTime", "p95"), datasource=CLOUDWATCH,
          description="ALB가 web 대상에 요청을 전달한 뒤 응답 헤더를 받기까지 걸린 시간입니다."),
    panel("웹 Pod CPU", "cores", prom(f'sum by(pod)(rate(container_cpu_usage_seconds_total{{{web_selector}}}[5m]))', "{{pod}}")),
    panel("웹 Pod 메모리", "bytes", prom(f'sum by(pod)(container_memory_working_set_bytes{{{web_selector}}})', "{{pod}}")),
], ("frontend",))

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with OUTPUT.open("w", encoding="utf-8", newline="\n") as out:
    out.write("""# Generated by scripts/gen-dev3-team-dashboards.py.
apiVersion: v1
kind: ConfigMap
metadata:
  name: keepgo-dev3-team-dashboards
  namespace: monitoring
  labels:
    grafana_dashboard: "1"
    app.kubernetes.io/part-of: keepgo
  annotations:
    grafana_folder: KeepGo V3
data:
""")
    for team_dashboard in (ai, backend, frontend):
        out.write(f'  {team_dashboard["uid"]}.json: |\n')
        for line in json.dumps(team_dashboard, ensure_ascii=False, indent=2).splitlines():
            out.write(f"    {line}\n")
