ARG REPO_WEB_BASE_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine
FROM ${REPO_WEB_BASE_IMAGE}

COPY config/web/default.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
