import json
import asyncio
import time
import traceback
import asyncpg
from datetime import datetime
from typing import Tuple, Optional, List, AsyncContextManager, Any
from ipapp.misc import mask_url_pwd
from pydantic.main import BaseModel
from ipapp.logger import wrap2span, Span
from ipapp.ctx import span
from iprpc import MethodExecutor
from ipapp.error import PrepareError
from ipapp.db.pg import Postgres
from ipapp import Component
from datetime import datetime, timedelta
from typing import Union, Optional, Callable
from contextlib import asynccontextmanager
import attr

TaskHandler = Union[Callable, str]
ETA = Union[datetime, float, int]

STATUS_PENDING = 'pending'
STATUS_IN_PROGRESS = 'in_progress'
STATUS_SUCCESSFUL = 'successful'
STATUS_ERROR = 'error'
STATUS_RETRY = 'retry'
STATUS_CANCELED = 'canceled'


CREATE_TABLE_QUERY = """\
CREATE TYPE {schema}.task_status AS ENUM
   ('pending',
    'progress',
    'successful',
    'error',
    'retry',
    'canceled');

CREATE SEQUENCE {schema}.task_id_seq;

CREATE TABLE {schema}.task
(
  id bigint NOT NULL DEFAULT nextval('{schema}.task_id_seq'::regclass),
  eta timestamp with time zone NOT NULL DEFAULT now(),
  name text NOT NULL,
  params jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  max_retries integer NOT NULL DEFAULT 0,
  retry_delay interval NOT NULL DEFAULT '00:01:00'::interval,
  status {schema}.task_status NOT NULL,
  last_stamp timestamp with time zone NOT NULL DEFAULT now(),
  retries integer,
  CONSTRAINT task_pkey PRIMARY KEY (id),
  CONSTRAINT task_empty_table_check CHECK (false) NO INHERIT,
  CONSTRAINT task_max_retries_check CHECK (max_retries >= 0),
  CONSTRAINT task_params_check CHECK (jsonb_typeof(params) = 'object'::text)
);

CREATE TABLE {schema}.task_pending
(
  id bigint NOT NULL DEFAULT nextval('{schema}.task_id_seq'::regclass),
  status {schema}.task_status NOT NULL DEFAULT 'pending'::{schema}.task_status,
  CONSTRAINT task_pending_pkey PRIMARY KEY (id),
  CONSTRAINT task_max_retries_check CHECK (max_retries >= 0),
  CONSTRAINT task_params_check CHECK (jsonb_typeof(params) = 'object'::text),
  CONSTRAINT task_pending_status_check CHECK (status = ANY (ARRAY[
      'pending'::{schema}.task_status,
      'retry'::{schema}.task_status,
      'progress'::{schema}.task_status]))
)
INHERITS ({schema}.task);

CREATE TABLE {schema}.task_arch
(
  id bigint NOT NULL DEFAULT nextval('{schema}.task_id_seq'::regclass),
  status {schema}.task_status NOT NULL 
      DEFAULT 'canceled'::{schema}.task_status,
  CONSTRAINT task_arch_pkey PRIMARY KEY (id),
  CONSTRAINT task_max_retries_check CHECK (max_retries >= 0),
  CONSTRAINT task_params_check CHECK (jsonb_typeof(params) = 'object'::text),
  CONSTRAINT task_pending_status_check CHECK (status <> ALL (ARRAY[
      'pending'::{schema}.task_status,
      'retry'::{schema}.task_status,
      'progress'::{schema}.task_status]))
)
INHERITS ({schema}.task);

CREATE TABLE {schema}.task_log
(
  id bigserial NOT NULL,
  task_id bigint NOT NULL,
  eta timestamp with time zone NOT NULL,
  started timestamp with time zone,
  finished timestamp with time zone,
  result jsonb,
  error text,
  traceback text,
  CONSTRAINT task_log_pkey PRIMARY KEY (id)
);

CREATE INDEX task_pending_eta_idx
  ON {schema}.task_pending
  USING btree
  (eta)
  WHERE status = ANY (ARRAY['pending'::{schema}.task_status, 
                            'retry'::{schema}.task_status]);
  
CREATE INDEX task_log_task_id_idx
  ON {schema}.task_log
  USING btree
  (task_id);
"""


class Task(BaseModel):
    id: int
    eta: datetime
    name: str
    params: dict
    max_retries: int
    retry_delay: timedelta
    status: str
    retries: Optional[int]


class Retry(Exception):
    def __init__(self, err: Exception):
        self.err = err

    def __str__(self):
        return 'Retry: %r' % (str(self.err) or repr(self.err))


class TaskManagerConfig(BaseModel):
    db_url: Optional[str] = None
    db_schema: str = 'main'
    db_connect_max_attempts: int = 10
    db_connect_retry_delay: float = 1.0
    batch_size: int = 1
    max_scan_interval: float = 60.0
    idle: bool = False


class TaskManager(Component):
    def __init__(self, api: object, cfg: TaskManagerConfig) -> None:
        self._executor: MethodExecutor = MethodExecutor(api)
        self.cfg = cfg
        self._stopping = False
        self._scan_fut: Optional[asyncio.Future] = None
        self.stamp_early: float = 0.0
        self._lock: Optional[asyncio.Lock] = None
        self._conn: Optional[asyncpg.Connection] = None
        self._db: Optional[Db] = None

    @property
    def _masked_url(self) -> Optional[str]:
        if self.cfg.db_url is not None:
            return mask_url_pwd(self.cfg.db_url)
        return None

    async def prepare(self) -> None:
        if self.app is None:  # pragma: no cover
            raise UserWarning('Unattached component')

        self._lock = asyncio.Lock(loop=self.loop)

        for i in range(self.cfg.db_connect_max_attempts):
            try:
                self.app.log_info("Connecting to %s", self._masked_url)
                self._conn = await asyncpg.connect(dsn=self.cfg.db_url)
                await Postgres._conn_init(self._conn)  # noqa
                self.app.log_info("Connected to %s", self._masked_url)
                return
            except Exception as e:
                self.app.log_err(str(e))
                await asyncio.sleep(self.cfg.db_connect_retry_delay)
        raise PrepareError("Could not connect to %s" % self._masked_url)

    async def start(self) -> None:
        if self.app is None:  # pragma: no cover
            raise UserWarning('Unattached component')

        if self._conn is None:  # pragma: no cover
            raise UserWarning

        self._db = Db(self._conn, self.cfg.db_schema)
        if not self.cfg.idle:
            self._scan_fut = asyncio.ensure_future(
                self._scan(), loop=self.loop
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._lock is None:
            return
        await self._lock
        if self._scan_fut is not None:
            if not self._scan_fut.done():
                await self._scan_fut

    async def health(self) -> None:
        await self._db.health(lock=True)

    async def schedule(
        self,
        func: TaskHandler,
        params: dict,
        eta: Optional[ETA] = None,
        max_retries: int = 0,
        retry_delay: float = 60.0,
    ) -> int:
        if self._conn is None:  # pragma: no cover
            raise UserWarning

        if not isinstance(func, str):
            if not hasattr(func, '__rpc_name__'):  # pragma: no cover
                raise UserWarning('Invalid task handler')
            func_name = getattr(func, '__rpc_name__')
        else:
            func_name = func

        eta_dt: Optional[datetime] = None
        if isinstance(eta, int) or isinstance(eta, float):
            eta_dt = datetime.fromtimestamp(eta)
        elif isinstance(eta, datetime):
            eta_dt = eta
        elif eta is not None:  # pragma: no cover
            raise UserWarning

        task_id, task_delay = await self._db.task_add(
            eta_dt, func_name, params, max_retries, retry_delay, lock=True
        )

        eta_float = self.loop.time() + task_delay
        self.stamp_early = eta_float
        self.loop.call_at(eta_float, self._scan_later, eta_float)

        return task_id

    @wrap2span(name='dbtm:Scan', kind=Span.KIND_SERVER)
    async def _scan(self) -> List[int]:
        if self.app is None or self._lock is None:  # pragma: no cover
            raise UserWarning
        if self._stopping:
            return []

        with await self._lock:
            delay = 1.0  # default: 1 second
            try:
                tasks, delay = await self._search_and_exec()
                if len(tasks) == 0:
                    span.skip()
                return [task.id for task in tasks]
            except Exception as err:
                span.error(err)
                self.app.log_err(err)
            finally:
                self._scan_fut = None
                if not self._stopping:
                    span.annotate('next_scan', 'next: %s' % delay)
                    eta = self.loop.time() + delay
                    self.stamp_early = eta
                    self.loop.call_at(eta, self._scan_later, eta)
            return []

    def _scan_later(self, when):
        if when != self.stamp_early:
            return
        if self._db is None:  # pragma: no cover
            raise UserWarning
        if self._stopping:
            return
        if not self.cfg.idle:
            self._scan_fut = asyncio.ensure_future(
                self._scan(), loop=self.loop
            )

    async def _search_and_exec(self) -> Tuple[List[Task], float]:
        if self._db is None:  # pragma: no cover
            raise UserWarning
        async with self._db.transaction():

            tasks = await self._db.task_search(self.cfg.batch_size, lock=False)
            if len(tasks) == 0:
                next_delay = await self._db.task_next_delay(lock=False)
                if (
                    next_delay is None
                    or next_delay >= self.cfg.max_scan_interval
                ):
                    return tasks, self.cfg.max_scan_interval
                if next_delay <= 0:
                    return tasks, 0
                return tasks, next_delay

        coros = [self._exec(span.trace_id, task) for task in tasks]
        await asyncio.gather(*coros, loop=self.loop)

        return tasks, 0

    @wrap2span(name='dbtm:exec', kind=Span.KIND_SERVER)
    async def _exec(self, parent_trace_id: str, task: Task):
        if self._db is None or self._executor is None:  # pragma: no cover
            raise UserWarning

        span.name = 'dbtm:%s' % task.name
        span.tag('dbtm.parent_trace_id', parent_trace_id)
        span.tag('dbtm.task_id', task.id)
        span.tag('dbtm.task_name', task.name)
        try:
            time_begin = time.time()
            res = await self._executor.call_parsed(task.name, task.params, {})
            time_finish = time.time()

            err_str: Optional[str] = None
            err_trace: Optional[str] = None
            if res.error is not None:
                if res.error.parent is not None:
                    if isinstance(res.error.parent, Retry):
                        err_str = str(res.error.parent.err)
                    else:
                        err_str = str(res.error.parent)
                else:
                    err_str = str(res.error)
                err_trace = res.error.trace

                span.error(res.error)
                self.app.log_err(res.error)

            await self._db.task_log_add(
                task.id,
                task.eta,
                time_begin,
                time_finish,
                res.result,
                err_str,
                err_trace,
                lock=True,
            )

            if task.retries is None:
                retries = 0
            else:
                retries = task.retries + 1

            if res.error is not None:
                if isinstance(res.error.parent, Retry):
                    if retries >= task.max_retries:
                        await self._db.task_move_arch(
                            task.id, STATUS_ERROR, retries, lock=True
                        )
                    else:
                        await self._db.task_retry(
                            task.id,
                            retries,
                            task.retry_delay.total_seconds(),
                            lock=True,
                        )
                else:
                    await self._db.task_move_arch(
                        task.id, STATUS_ERROR, retries, lock=True
                    )
            else:
                await self._db.task_move_arch(
                    task.id, STATUS_SUCCESSFUL, retries, lock=True
                )
        except Exception as err:
            span.error(err)
            raise


class Db:
    def __init__(self, conn: asyncpg.Connection, db_schema: str) -> None:
        self._lock = asyncio.Lock()
        self._conn: asyncpg.Connection = conn
        self._db_schema = db_schema

    async def _fetch(
        self, query, *args, timeout: Optional[float] = None, lock: bool = False
    ) -> List[asyncpg.Record]:
        if self._conn is None:  # pragma: no cover
            raise UserWarning
        if lock and self._conn.is_in_transaction():  # pragma: no cover
            raise UserWarning
        if lock:
            async with self._lock:
                return await self._conn.fetch(query, *args, timeout=timeout)
        else:
            return await self._conn.fetch(query, *args, timeout=timeout)

    async def _fetchrow(
        self, query, *args, timeout: Optional[float] = None, lock: bool = False
    ) -> Optional[asyncpg.Record]:
        if self._conn is None:  # pragma: no cover
            raise UserWarning
        if lock and self._conn.is_in_transaction():  # pragma: no cover
            raise UserWarning
        if lock:
            async with self._lock:
                return await self._conn.fetchrow(query, *args, timeout=timeout)
        else:
            return await self._conn.fetchrow(query, *args, timeout=timeout)

    async def _execute(
        self, query, *args, timeout: Optional[float] = None, lock: bool = False
    ) -> None:
        if self._conn is None:  # pragma: no cover
            raise UserWarning
        if lock and self._conn.is_in_transaction():  # pragma: no cover
            raise UserWarning
        if lock:
            async with self._lock:
                await self._conn.execute(query, *args, timeout=timeout)
        else:
            await self._conn.execute(query, *args, timeout=timeout)

    @asynccontextmanager
    async def transaction(
        self, isolation='read_committed', readonly=False, deferrable=False
    ) -> AsyncContextManager['Db']:
        async with self._lock:
            async with self._conn.transaction(
                isolation=isolation, readonly=readonly, deferrable=deferrable
            ):
                yield self

    async def task_add(
        self,
        eta: Optional[datetime],
        name: str,
        params: dict,
        max_retries: int,
        retry_delay: float,
        *,
        lock: bool = False,
    ) -> Tuple[int, float]:
        query = (
            "INSERT INTO %s.task_pending"
            "(eta,name,params,max_retries,retry_delay) "
            "VALUES(COALESCE($1, NOW()),$2,$3,$4,"
            "make_interval(secs=>$5::float)) "
            "RETURNING id, "
            "greatest(extract(epoch from NOW() - eta), 0) as delay"
        ) % self._db_schema

        res = await self._fetchrow(
            query, eta, name, params, max_retries, retry_delay, lock=lock
        )
        if res is None:  # pragma: no cover
            raise UserWarning
        return res['id'], res['delay']

    async def task_search(
        self, batch_size: int, *, lock: bool = False
    ) -> List[Task]:
        query = (
            "UPDATE %s.task_pending SET status='progress',last_stamp=NOW() "
            "WHERE id IN ("
            "SELECT id FROM %s.task_pending "
            "WHERE eta<NOW() AND "
            "status=ANY(ARRAY['pending'::%s.task_status,"
            "'retry'::%s.task_status])"
            "LIMIT $1 FOR UPDATE SKIP LOCKED) "
            "RETURNING "
            "id,eta,name,params,max_retries,retry_delay,status,retries"
        ) % (self._db_schema, self._db_schema,
             self._db_schema, self._db_schema)

        res = await self._fetch(query, batch_size, lock=lock)

        return [Task(**dict(row)) for row in res]

    async def task_next_delay(self, *, lock: bool = False) -> Optional[float]:

        query = (
            "SELECT EXTRACT(EPOCH FROM eta-NOW())t "
            "FROM %s.task_pending "
            "WHERE "
            "status=ANY(ARRAY['pending'::%s.task_status,"
            "'retry'::%s.task_status])"
            "ORDER BY eta "
            "LIMIT 1 "
            "FOR SHARE SKIP LOCKED"
        ) % (self._db_schema, self._db_schema, self._db_schema)
        res = await self._fetchrow(query, lock=lock)
        if res:
            return res['t']
        return None

    async def task_retry(
        self,
        task_id: int,
        retries: int,
        eta_delay: Optional[float],
        *,
        lock: bool = False,
    ) -> None:
        query = (
            'UPDATE %s.task_pending SET status=$2,retries=$3,'
            'eta=COALESCE(NOW()+make_interval(secs=>$4::float),eta),'
            'last_stamp=NOW() '
            'WHERE id=$1'
        ) % self._db_schema

        await self._execute(
            query, task_id, STATUS_RETRY, retries, eta_delay, lock=lock
        )

    async def task_move_arch(
        self, task_id: int, status: str, retries: int, *, lock: bool = False
    ) -> None:

        query = (
            'WITH del AS (DELETE FROM %s.task_pending WHERE id=$1 '
            'RETURNING id,eta,name,params,max_retries,retry_delay)'
            'INSERT INTO %s.task_arch'
            '(id,eta,name,params,max_retries,retry_delay,status,'
            'retries,last_stamp)'
            'SELECT '
            'id,eta,name,params,max_retries,retry_delay,$2,$3,NOW() '
            'FROM del'
        ) % (self._db_schema, self._db_schema)
        await self._execute(query, task_id, status, retries, lock=lock)

    async def task_log_add(
        self,
        task_id: int,
        eta: datetime,
        started: float,
        finished: float,
        result: Any,
        error: Optional[str],
        trace: Optional[str],
        *,
        lock: bool = False,
    ) -> None:
        query = (
            'INSERT INTO %s.task_log'
            '(task_id,eta,started,finished,result,error,traceback)'
            'VALUES($1,$2,to_timestamp($3),to_timestamp($4),'
            '$5::text::jsonb,$6,$7)'
        ) % self._db_schema
        js = json.dumps(result) if result is not None else None

        await self._execute(
            query, task_id, eta, started, finished, js, error, trace, lock=lock
        )

    async def health(self, *, lock: bool = False):
        await self._execute('SELECT 1', lock=lock)
