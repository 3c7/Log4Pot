# A honeypot for the Log4Shell vulnerability (CVE-2021-44228)

import json
import re
import socket
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Any, List, Optional
from uuid import uuid4

from expression_parser import parse
from payloader import Payloader, pycurl_available

try:
    from azure.storage.blob import BlobServiceClient

    azure_import = True
except ImportError:
    print(
        "Azure dependencies not installed, logging to blob storage not available.",
        file=sys.stderr
    )
    azure_import = False

re_exploit = re.compile("\${.*}")

@dataclass
class Logger:
    log_file: str
    log_blob: Optional["azure.storage.blob.BlobClient"]

    def __post_init__(self):
        self.f = open(self.log_file, "a")

    def log(self, logtype: str, message: str, **kwargs):
        d = {
            "type": logtype,
            "timestamp": datetime.utcnow().isoformat(),
            **kwargs,
        }
        j = json.dumps(d) + "\n"
        self.f.write(j)
        self.f.flush()
        if self.log_blob is not None:
            self.log_blob.append_block(j)

    def log_start(self):
        self.log("start", "Log4Pot started")

    def log_request(self, server_port, client, port, request, headers, uuid):
        self.log("request", "A request was received", correlation_id=str(uuid), server_port=server_port, client=client,
                 port=port, request=request, headers=dict(headers))

    def log_exploit(self, location, payload, uuid):
        self.log("exploit", "Exploit detected", correlation_id=str(uuid), location=location, payload=payload,
                 deobfuscated_payload=parse(payload))

    def log_payload(self, uuid, **kwargs):
        self.log("payload", "Payload downloaded", correlation_id=str(uuid), **kwargs)

    def log_exception(self, e: Exception, **kwargs):
        self.log("exception", "Exception occurred", exception=str(e), **kwargs)

    def log_end(self):
        self.log("end", "Log4Pot stopped")

    def close(self):
        self.log_end()
        self.f.close()


class Log4PotHTTPRequestHandler(BaseHTTPRequestHandler):
    def do(self):
        # If a custom server header is set, overwrite the version_string() function
        if self.server.server_header:
            self.version_string = lambda: self.server.server_header
        self.uuid = uuid4()
        self.send_response(200)
        self.send_header("Content-Type", "text/json")
        self.end_headers()
        self.wfile.write(bytes(f'{{ "status": "ok", "id": "{self.uuid}" }}', "utf-8"))

        self.logger = self.server.logger
        self.logger.log_request(self.server.server_address[1], *self.client_address, self.requestline, self.headers,
                                self.uuid)
        self.find_exploit("request", self.requestline)
        for header, value in self.headers.items():
            self.find_exploit(f"header-{header}", value)

    def find_exploit(self, location: str, content: str) -> bool:
        if (m := re_exploit.search(content)):
            logger.log_exploit(location, m.group(0), self.uuid)

            if self.server.payloader:
                try:
                    data = self.server.payloader.process_payloads(
                        parse(m.group(0)),
                    )
                    self.logger.log_payload(self.uuid, **data)
                except Exception as e:
                    self.logger.log_exception(e, correlation_id=str(self.uuid))

    def __getattribute__(self, __name: str) -> Any:
        if __name.startswith("do_"):
            return self.do
        else:
            return super().__getattribute__(__name)


class Log4PotHTTPServer(ThreadingHTTPServer):
    def __init__(self, logger: Logger, payloader : Payloader, server_header : str, *args, **kwargs):
        self.logger = logger
        self.payloader = payloader
        self.server_header = server_header
        super().__init__(*args, **kwargs)


class Log4PotServerThread(Thread):
    def __init__(self, logger: Logger, payloader : Payloader, server_header : str, port: int, *args, **kwargs):
        self.port = port
        self.server = Log4PotHTTPServer(
            logger,
            payloader,
            server_header,
            ("", port),
            Log4PotHTTPRequestHandler,
        )
        super().__init__(name=f"httpserver-{port}", *args, **kwargs)

    def run(self):
        try:
            self.server.serve_forever()
            self.server.server_close()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.log_exception(e)


class Log4PotArgumentParser(ArgumentParser):
    def convert_arg_line_to_args(self, arg_line: str) -> List[str]:
        return arg_line.split()


argparser = Log4PotArgumentParser(
    description="A honeypot for the Log4Shell vulnerability (CVE-2021-44228).",
    fromfile_prefix_chars="@",
)
argparser.add_argument("--port", "-p", action="append", type=int, help="Listening port")
argparser.add_argument("--log", "-l", type=str, default="log4pot.log", help="Log file")
argparser.add_argument("--blob-connection-string", "-b", help="Azure blob storage connection string.")
argparser.add_argument("--log-container", "-lc", type=str, default="logs", help="Azure blob container for logs.")
argparser.add_argument("--log-blob", "-lb", default=socket.gethostname() + ".log", help="Azure blob for logs.")
argparser.add_argument("--server-header", type=str, default=None, help="Replace the default server header.")
argparser.add_argument("--payloader", "-P", action="store_true", help="Download any analyze payloads from exploit attempts.")
argparser.add_argument("--download-dir", "-dd", type=str, help="Set a download directory for payloader. Only analysis is conducted ")
argparser.add_argument("--download-container", "-dc", type=str, help="Azure blob container for downloaded payloads.")
argparser.add_argument("--download-timeout", "-dt", type=int, default=10, help="Set download timeout for payloads.")

args = argparser.parse_args()
if args.port is None:
    print("No port specified!", file=sys.stderr)
    sys.exit(1)

if not pycurl_available and args.payloader:
        print("Payload analysis requested but no pycurl installed!")
        sys.exit(2)

if args.blob_connection_string is not None:
    if not azure_import:
        print("Azure logging requested but no azure package installed!")
        sys.exit(2)
    service_client = BlobServiceClient.from_connection_string(args.blob_connection_string)
    log_container = service_client.get_container_client(args.log_container)
    download_container = service_client.get_container_client(args.download_container)
    log_blob = log_container.get_blob_client(args.log_blob)
    log_blob.exists() or log_blob.create_append_blob()
else:
    log_blob = None
    download_container = None

logger = Logger(args.log, log_blob)
if args.payloader:
    payloader = Payloader(args.download_dir, download_container, args.download_timeout)
else:
    payloader = None

threads = [
    Log4PotServerThread(
        logger,
        payloader,
        args.server_header,
        port,
        )
    for port in args.port
]
logger.log_start()

for thread in threads:
    thread.start()
    print(f"Started Log4Pot server on port {thread.port}.")

for thread in threads:
    thread.join()
    print(f"Stopped Log4Pot server on port {thread.port}.")

logger.close()
