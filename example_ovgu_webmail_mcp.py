#pip install "mcp[cli]<2"
#export OVGU_IMAP_USER='your_username'
#export OVGU_IMAP_PASSWORD='your_password'
from pathlib import Path
import dotenv
dotenv.load_dotenv(Path(__file__).with_name(".env"))

import asyncio, ovgu_webmail_mcp as s
async def m():
    print(await s.webmail_list_folders(s.ListFoldersInput()))
    print(await s.webmail_list_messages(s.ListMessagesInput(limit=5)))
asyncio.run(m())