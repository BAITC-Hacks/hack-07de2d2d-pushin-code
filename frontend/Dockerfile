# Windcast frontend: static HTML + vanilla JS behind nginx on 0.0.0.0:3000.
# Build context is this directory:  docker build -t windcast-frontend frontend/
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html app.js /usr/share/nginx/html/

EXPOSE 3000
