{{/*
Expand the name of the chart.
*/}}
{{- define "llamator-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "llamator-mcp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "llamator-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "llamator-mcp.labels" -}}
helm.sh/chart: {{ include "llamator-mcp.chart" . }}
{{ include "llamator-mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "llamator-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llamator-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
API selector labels
*/}}
{{- define "llamator-mcp.api.selectorLabels" -}}
{{ include "llamator-mcp.selectorLabels" . }}
app.kubernetes.io/component: api
{{- end }}

{{/*
Worker selector labels
*/}}
{{- define "llamator-mcp.worker.selectorLabels" -}}
{{ include "llamator-mcp.selectorLabels" . }}
app.kubernetes.io/component: worker
{{- end }}

{{/*
Redis selector labels
*/}}
{{- define "llamator-mcp.redis.selectorLabels" -}}
{{ include "llamator-mcp.selectorLabels" . }}
app.kubernetes.io/component: redis
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "llamator-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "llamator-mcp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Return the proper namespace
*/}}
{{- define "llamator-mcp.namespace" -}}
{{- default .Release.Namespace .Values.global.namespace }}
{{- end }}

{{/*
Redis fullname
*/}}
{{- define "llamator-mcp.redis.fullname" -}}
{{- printf "%s-redis" (include "llamator-mcp.fullname" .) }}
{{- end }}

{{/*
Redis host
*/}}
{{- define "llamator-mcp.redis.host" -}}
{{- printf "%s.%s.svc.cluster.local" (include "llamator-mcp.redis.fullname" .) (include "llamator-mcp.namespace" .) }}
{{- end }}

{{/*
Redis DSN
*/}}
{{- define "llamator-mcp.redis.dsn" -}}
{{- printf "redis://%s:%d/0" (include "llamator-mcp.redis.host" .) (int .Values.redis.service.port) }}
{{- end }}

{{/*
API fullname
*/}}
{{- define "llamator-mcp.api.fullname" -}}
{{- printf "%s-api" (include "llamator-mcp.fullname" .) }}
{{- end }}

{{/*
Worker fullname
*/}}
{{- define "llamator-mcp.worker.fullname" -}}
{{- printf "%s-worker" (include "llamator-mcp.fullname" .) }}
{{- end }}

{{/*
ConfigMap name
*/}}
{{- define "llamator-mcp.configmap.name" -}}
{{- printf "%s-config" (include "llamator-mcp.fullname" .) }}
{{- end }}

{{/*
Artifacts PVC name
*/}}
{{- define "llamator-mcp.artifacts.pvc.name" -}}
{{- printf "%s-artifacts" (include "llamator-mcp.fullname" .) }}
{{- end }}

{{/*
Redis PVC name
*/}}
{{- define "llamator-mcp.redis.pvc.name" -}}
{{- printf "%s-redis-data" (include "llamator-mcp.fullname" .) }}
{{- end }}

