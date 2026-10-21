# 이번 검증에 사용한 로컬 PostgreSQL

기존 사용자 DB 대신 별도 PostgreSQL 17 클러스터를 생성해 전체 원천 적재를 검증했다.

- 데이터 디렉터리: `/private/tmp/battery-etl-pg-20261017`
- listen 주소/port: `127.0.0.1:55437`
- 원천 적재 DB: `battery_etl_validation`
- DB 역할: `u079`
- 로그: `/private/tmp/battery-etl-pg-20261017/server.log`
- 바이너리: `/opt/homebrew/opt/postgresql@17/bin/`

검증 완료 후 이 검증용 서버는 중지한다. 데이터 디렉터리는 삭제하지 않는다. 임시 경로이므로 영구 운영 저장소/백업으로 간주하지 않는다. 원본 MAT는 프로젝트 루트에 그대로 있다. 인증은 로컬 검증용 trust로 초기화했으며 운영 인증 설정은 별도 범위다.

같은 컴퓨터에서 검증 DB를 다시 열려면:

```bash
/opt/homebrew/opt/postgresql@17/bin/pg_ctl \
  -D /private/tmp/battery-etl-pg-20261017 \
  -l /private/tmp/battery-etl-pg-20261017/server.log \
  -o '-p 55437 -k /private/tmp -h 127.0.0.1' start

export DATABASE_URL='postgresql://u079@127.0.0.1:55437/battery_etl_validation'
uv run battery-etl run 2017-05-12_batchdata_updated_struct_errorcorrect.mat --batch batch1 --load

/opt/homebrew/opt/postgresql@17/bin/psql \
  -h 127.0.0.1 -p 55437 -U u079 -d battery_etl_validation \
  -c 'SELECT count(*) FROM cycles;'

/opt/homebrew/opt/postgresql@17/bin/pg_ctl \
  -D /private/tmp/battery-etl-pg-20261017 stop -m fast
```

같은 파일의 재실행은 `skipped`로 반환된다. 다른 컴퓨터에서는 README 절차에 따라 별도의 PostgreSQL DB를 준비한다.
