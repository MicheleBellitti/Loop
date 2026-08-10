# One image, eight commands. The services differ by which entry point they run,
# not by what they contain, so there is one layer cache and one thing to patch.
FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json tsconfig.base.json tsconfig.json ./
COPY packages ./packages
COPY services ./services
COPY client ./client
RUN npm ci

RUN npx tsc --build && npm run build -w @loop/client

# Drop the build toolchain and the client's dev tree.
FROM node:22-alpine

WORKDIR /app
ENV NODE_ENV=production

COPY package.json package-lock.json ./
COPY --from=build /app/packages ./packages
COPY --from=build /app/services ./services
COPY --from=build /app/client/dist ./client/dist
COPY rules ./rules
RUN npm ci --omit=dev && npm cache clean --force

# Nothing here needs root.
USER node

# Every service exposes /health on its own internal port; compose does not
# publish any of them.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.HEALTH_PORT||3000)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "services/gateway/dist/index.js"]
