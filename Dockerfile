FROM condaforge/miniforge3:26.7.2-0

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY environment.yml /tmp/environment.yml

RUN conda env update --name base --file /tmp/environment.yml \
    && conda clean --all --yes \
    && rm /tmp/environment.yml \
    && mkdir -p /home/app \
    && chown "${APP_UID}:${APP_GID}" /app /home/app

ENV HOME=/home/app

USER ${APP_UID}:${APP_GID}

# *!*! Cache required DuckDB extensions in the application user's home. Retry
# *!*! transient repository failures and report the extension being installed.
RUN python <<'PY'
import time

import duckdb


connection = duckdb.connect()
try:
    for extension in ('spatial', 'httpfs'):
        print(f'Installing DuckDB extension: {extension}', flush=True)
        for attempt in range(3):
            try:
                connection.execute(f'INSTALL {extension}')
                break
            except duckdb.IOException:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
finally:
    connection.close()
PY

COPY --chown=${APP_UID}:${APP_GID} . /app

EXPOSE 8000 8501

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8501"]
