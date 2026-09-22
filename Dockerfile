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

RUN python -c "import duckdb; connection = duckdb.connect(); connection.execute('INSTALL spatial'); connection.execute('INSTALL httpfs'); connection.execute('INSTALL postgres'); connection.close()"

COPY --chown=${APP_UID}:${APP_GID} . /app

EXPOSE 8000 8501

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8501"]
