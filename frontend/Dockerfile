# Stage 1: Builder
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package files
COPY package.json yarn.lock* package-lock.json* ./

# Install dependencies. The VPS network can be slow, so give Yarn more time;
# npm fallback needs legacy peer handling for the current lockfile.
RUN yarn config set registry https://registry.npmjs.org \
    && yarn install --frozen-lockfile --network-timeout 600000 --network-concurrency 1 \
    || npm ci --legacy-peer-deps --fetch-retries=5 --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000 --fetch-timeout=600000

# Copy source code
COPY . .

# Build the app with environment variables
ARG REACT_APP_API_BASE=/api
ENV REACT_APP_API_BASE=${REACT_APP_API_BASE}
ENV NODE_ENV=production

RUN yarn build || npm run build

# Stage 2: Production server
FROM nginx:alpine

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built app from builder
COPY --from=builder /app/build /usr/share/nginx/html

# Expose ports
EXPOSE 80 443

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --spider -q http://127.0.0.1/ || exit 1

# Start nginx
CMD ["nginx", "-g", "daemon off;"]
