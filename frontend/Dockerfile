# Windcast frontend: React app built with Vite, served by nginx on 0.0.0.0:3000.
# /api/* and /health are proxied to the backend (nginx.conf). Build context is this directory.
FROM node:20.11.1-alpine3.19 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 3000
