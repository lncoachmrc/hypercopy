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
RUN printf '%s\n' '#!/bin/sh' 'set -eu' 'envsubst '\''$PORT'\'' < /etc/nginx/nginx.conf.tpl > /etc/nginx/conf.d/default.conf' 'envsubst '\''$API_BASE_URL $WS_URL'\'' < /usr/share/nginx/html/config.template.js > /usr/share/nginx/html/config.js' 'exec nginx -g "daemon off;"' > /entrypoint.sh && chmod +x /entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
