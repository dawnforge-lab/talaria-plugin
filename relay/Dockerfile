FROM node:22-alpine

ENV NODE_ENV=production
WORKDIR /app

COPY --chown=node:node package.json ./
COPY --chown=node:node src ./src
# The default DATA_PATH's directory, node's own, for a named volume or no
# mount at all (a bind mount keeps the host directory's owner).
RUN mkdir -p /var/lib/talaria-relay && chown node:node /var/lib/talaria-relay

USER node
EXPOSE 9120
CMD ["node", "src/server.mjs"]
