FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.29-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY config.template.js /usr/share/nginx/html/config.template.js
COPY nginx.conf /etc/nginx/nginx.conf.tpl
RUN printf '%s\n' \
  '#!/bin/sh' \
  'set -eu' \
  ': "${API_PROXY_UPSTREAM:=http://api.railway.internal:8080}"' \
  'RAW_DNS_RESOLVER="$(awk '\''/^nameserver[[:space:]]+/ {print $2; exit}'\'' /etc/resolv.conf)"' \
  ': "${RAW_DNS_RESOLVER:=127.0.0.11}"' \
  'case "$RAW_DNS_RESOLVER" in *:*) DNS_RESOLVER="[$RAW_DNS_RESOLVER]" ;; *) DNS_RESOLVER="$RAW_DNS_RESOLVER" ;; esac' \
  'export API_PROXY_UPSTREAM DNS_RESOLVER' \
  'envsubst '\''$PORT $API_PROXY_UPSTREAM $DNS_RESOLVER'\'' < /etc/nginx/nginx.conf.tpl > /etc/nginx/conf.d/default.conf' \
  'envsubst '\''$API_BASE_URL $WS_URL $REOWN_PROJECT_ID'\'' < /usr/share/nginx/html/config.template.js > /usr/share/nginx/html/config.js' \
  'nginx -t' \
  'exec nginx -g "daemon off;"' \
  > /entrypoint.sh && chmod +x /entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
