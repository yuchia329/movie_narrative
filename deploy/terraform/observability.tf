# The ServiceMonitors + PrometheusRule live in the kustomize overlay (observability.yaml),
# applied by the kustomization provider — NOT as kubernetes_manifest, which would require the
# cluster reachable AND the CRDs registered at PLAN time (fragile in CI / first apply).
#
# The Grafana dashboard ConfigMap stays here because it must live in the `monitoring`
# namespace (where the kube-prometheus-stack Grafana sidecar watches), which the recap
# overlay's `namespace: recap` would otherwise override. kubernetes_config_map is a typed
# resource, so it has no plan-time OpenAPI dependency.
resource "kubernetes_config_map" "dashboard" {
  metadata {
    name      = "recap-dashboards"
    namespace = "monitoring"
    labels    = { grafana_dashboard = "1" }
  }
  data = {
    "recap.json" = file("${path.module}/../grafana/dashboards/recap.json")
  }
}
