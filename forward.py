"""droidhub-net：网络设备的 adb 转发。ADB 服务只在 internal 网络上，网络设备经这里连。

按配置文件里网络设备的顺序，在 5555 起的端口上各开一路转发到设备的 address。
配置文件变了就退出，由容器的 restart 拉起来按新配置转发。
"""
import asyncio
import logging
import sys

import config

CHECK_EVERY = 5
CONNECT_TIMEOUT = 10

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%m-%d %H:%M:%S')
log = logging.getLogger('forward')


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


def handler(host, port):
    async def handle(client_reader, client_writer):
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as e:
            log.info('连不上 %s:%s：%s', host, port, e)
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, writer), pipe(reader, client_writer))
    return handle


async def main():
    seen = config.mtime()
    try:
        table = config.forwards(config.load())
    except ValueError as e:
        log.info('配置有错：%s', e)
        table = []
    servers = []
    for listen, host, port in table:
        servers.append(await asyncio.start_server(handler(host, int(port)), '0.0.0.0', listen))
        log.info('转发 %d → %s:%s', listen, host, port)
    if not table:
        log.info('没有网络设备')
    while True:
        await asyncio.sleep(CHECK_EVERY)
        if config.mtime() != seen:
            log.info('配置文件改了，重启生效')
            for server in servers:
                server.close()
            sys.exit(0)


if __name__ == '__main__':
    asyncio.run(main())
