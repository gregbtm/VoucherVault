# Minify the hand-written frontend JS (Phase 88) - production image only.
# The git-tracked source under myapp/static/assets/js/ stays unminified so
# Vitest (Phase 87) keeps testing the real, readable source, never this
# stage's output; a local `npm run build` writes the same minified files
# to a gitignored dist/js/ that nothing else reads.
FROM node:22-slim AS jsbuild
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY myapp/static/assets/js ./myapp/static/assets/js
RUN npm run build

FROM python:3.12.13-slim

# define environment; CI (.github/workflows/conventional-commits.yml) passes
# --build-arg VERSION=X.Y.Z on every push to main. Left unset (not "0") when
# built without that build-arg - e.g. a Portainer git-based build - so
# settings.py falls back to the VERSION file below instead of a bogus "0".
ARG VERSION
ENV VERSION=$VERSION
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# update and install software + create necessary dirs
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    fonts-dejavu-core \
    ghostscript \
    gcc \
    libc6-dev \
    netcat-openbsd \
    media-types \
    postgresql-client \
    tesseract-ocr \
    unzip &&\
    mkdir -p /opt/app/docker/ /opt/app/database /opt/app/logs /opt/app/locale

# define workdir
WORKDIR /opt/app

# copy relevant source code
COPY myproject /opt/app/myproject
COPY myapp /opt/app/myapp
COPY api /opt/app/api
COPY notify /opt/app/notify
COPY imports /opt/app/imports
COPY ocr /opt/app/ocr
COPY manage.py /opt/app/manage.py
COPY VERSION /opt/app/VERSION
COPY UPSTREAM_VERSION /opt/app/UPSTREAM_VERSION
COPY requirements.txt /opt/app/requirements.txt
COPY /docker/entrypoint.sh /opt/app/docker/entrypoint.sh
COPY /docker/docker_uwsgi.ini /opt/app/docker/docker_uwsgi.ini
COPY locale /opt/app/locale
COPY docs /opt/app/docs

# overlay the minified JS from the jsbuild stage onto the same served
# paths (uwsgi's static-map serves myapp/static/ directly - see
# docker/docker_uwsgi.ini) - same filenames/URLs, no template changes.
# zxing.js (already a minified vendor bundle) and
# offline-integration-examples.js (unused, not referenced by any
# template) are deliberately left out of the build list in package.json.
# Vitest's *.test.js files have no reason to ship in the image either.
COPY --from=jsbuild /build/dist/js/ /opt/app/myapp/static/assets/js/
RUN rm -f /opt/app/myapp/static/assets/js/*.test.js

# install dependencies, fix permissions
RUN pip install --upgrade pip && \
    pip3 install -r requirements.txt --cache-dir /opt/app/pip_cache && \
    chown -R www-data:www-data /opt/app && \
    chmod -R 770 /opt/app && \
    chmod +x /opt/app/docker/entrypoint.sh
    
# run container as low privileged user
USER www-data

# start web server
ENTRYPOINT ["/opt/app/docker/entrypoint.sh"]
EXPOSE 8000
STOPSIGNAL SIGTERM
