{{- define "astrocyte-gateway-py.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "astrocyte-gateway-py.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "astrocyte-gateway-py.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "astrocyte-gateway-py.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{ include "astrocyte-gateway-py.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "astrocyte-gateway-py.selectorLabels" -}}
app.kubernetes.io/name: {{ include "astrocyte-gateway-py.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
