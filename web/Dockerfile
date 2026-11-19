# Build the dashboard once, then serve the static files from nginx with a strict Content-Security-Policy.
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# The API address is baked into the bundle at build time (Vite reads it as import.meta.env.VITE_API_URL).
ARG VITE_API_URL=http://127.0.0.1:8000
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:1.27-alpine
# nginx fills ${API_ORIGIN} into the template when the container starts (the CSP must name the API).
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
ENV API_ORIGIN=http://127.0.0.1:8000
EXPOSE 80
