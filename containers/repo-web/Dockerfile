ARG REPO_WEB_BASE_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine
FROM ${REPO_WEB_BASE_IMAGE}

COPY config/web/default.conf /etc/nginx/conf.d/default.conf
COPY containers/repo-web/html /usr/share/nginx/html

EXPOSE 8080
