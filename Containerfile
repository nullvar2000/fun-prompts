FROM docker.io/nginxinc/nginx-unprivileged:alpine

COPY apps.json /usr/share/nginx/html/apps.json
COPY gallery/ /usr/share/nginx/html/gallery/
COPY apps/ /usr/share/nginx/html/apps/
COPY prompts/ /usr/share/nginx/html/prompts/
COPY nginx/default.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
