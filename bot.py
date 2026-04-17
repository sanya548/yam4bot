import logging
import hashlib
import re
import sys
import asyncio
import aiohttp
import db
import yamusic
import config


from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from aiogram.types import (
    FSInputFile,
    InlineQuery,
    Message,
    ChosenInlineResult,
    InlineQueryResultAudio,
    URLInputFile,
    BufferedInputFile,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import InputMediaAudio
from aiogram.filters import Command

TOKEN = config.TG_TOKEN

def _resolve_log_level(raw: str) -> int:
    value = (raw or "").strip().upper()
    if not value:
        return logging.INFO
    if hasattr(logging, value):
        return getattr(logging, value)
    try:
        return int(value)
    except ValueError:
        return logging.INFO


dp = Dispatcher()
result_ids = {}  # Hash array Result_ID => Track_Id
_allowed_user_ids = set()


def _parse_allowed_user_ids(raw: str) -> set[int]:
    parts = re.split(r"[,\s]+", raw.strip())
    ids = set()
    for part in parts:
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logging.warning("Invalid user id in ALLOWED_USER_IDS: %s", part)
    return ids


if config.ALLOWED_USER_IDS:
    _allowed_user_ids = _parse_allowed_user_ids(config.ALLOWED_USER_IDS)


def _is_allowed_user(user_id: int) -> bool:
    # If allowlist is empty, deny by default for safety
    return user_id in _allowed_user_ids


def get_loading_markup(track_id: str | int):
    return InlineKeyboardMarkup(
        row_width=1,
        inline_keyboard=[
            [InlineKeyboardButton(text="Загружаем...", callback_data=str(track_id))]
        ],
    )


def ymtrack_as_inline_result(
    track: yamusic.YandexTrack,
    result_id: str | None = None,
    markup: InlineKeyboardMarkup | None = None,
) -> InlineQueryResultAudio:
    result_id = (
        result_id or hashlib.md5(str(track.yandex_track_id).encode()).hexdigest()
    )
    result_ids[result_id] = track.yandex_track_id
    return InlineQueryResultAudio(
        id=result_id,
        # getting actual download link is slow and hurts the UX, so a placeholder is used
        audio_url="https://cs12.spac.me/f/022155028048118178004245225041194015065048227225162088184137/1651935767/90406904/0/912c9cca2ba0f5da4d0df7cb4f7483ea/placeholder-spcs.me.mp3#asdasd",
        title=track.title,
        performer=track.artists,
        audio_duration=track.duration,
        # reply markup is required to have ability to edit the message later on
        reply_markup=get_loading_markup(track.yandex_track_id),
    )


@dp.message(Command(commands="upload_placeholder"))
async def upload_placeholder(message: Message, bot: Bot):
    if not _is_allowed_user(message.from_user.id):
        return
    result = await message.reply_audio(FSInputFile("./tagmp3_crank-2.mp3"))
    await message.reply(result.audio.file_id)


@dp.inline_query()
async def inline_search_audio(inline_query: InlineQuery, bot: Bot):
    if not _is_allowed_user(inline_query.from_user.id):
        await bot.answer_inline_query(
            inline_query.id,
            results=[],
            cache_time=5,
            is_personal=True,
        )
        return
    items = []
    query = inline_query.query

    if not query:
        pass
    elif query.startswith("https://"):
        # single-result mode
        yandex_match = re.match(
            r"https://music.yandex.(ru|by|kz|com)/album/(\d+)/track/(\d+)", query
        )
        if yandex_match:
            album_id, track_id = yandex_match.group(2), yandex_match.group(3)
            full_id = f"{track_id}:{album_id}"
            track = yamusic.get_track_data(full_id)
            items.append(ymtrack_as_inline_result(track))
    else:
        result = yamusic.search(query=query)
        if not result:
            logging.warning("Search returned no results for query: %s", query)
            result = []

        items = [ymtrack_as_inline_result(track) for track in result[:20]]

    await bot.answer_inline_query(inline_query.id, results=items, cache_time=5)


@dp.chosen_inline_result()
async def chosen_track(chosen_inline_result: ChosenInlineResult, bot: Bot):
    logging.info(f"Handling chosen track update: {chosen_inline_result.result_id} from user {chosen_inline_result.from_user.id}")
    if not _is_allowed_user(chosen_inline_result.from_user.id):
        logging.warning(f"User {chosen_inline_result.from_user.id} is NOT allowed")
        return
    result_id = chosen_inline_result.result_id
    if result_id in result_ids:
        try:
            track_id = result_ids[result_id]
            logging.info(f"Processing track_id: {track_id}")
            track = db.get(track_id)
            tg_file_id = str(track.tg_file_id) if track else None
            data = yamusic.get_track_data(track_id)
            logging.info(f"Got track data: {data.title} by {data.artists}")
            if not tg_file_id:
                # it could be that using URLInputFile will get throttled by Yandex Music, but it hasn't been the case
                # In the event this does happen, it's safer to use FSInputFile

                # can't edit message and upload a file at the same time, pre-upload is required
                # aiogram doesn't support uploading without sending atm. A dummy chat has to be created and configured
                download_link = data.get_download_link()
                logging.info(f"Downloading track from Yandex...")
                
                # Use Bot's proxy for download as well
                proxy = config.PROXY_URL if config.PROXY_URL else None
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(download_link, proxy=proxy) as resp:
                        if resp.status != 200:
                            raise Exception(f"Failed to download audio: status {resp.status}")
                        audio_bytes = await resp.read()
                
                audio_file = BufferedInputFile(audio_bytes, filename=f"{track_id}.mp3")

                # Thumbnail download
                thumbnail_file = None
                if data.cover_url:
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(data.cover_url, proxy=proxy) as resp:
                                if resp.status == 200:
                                    thumb_bytes = await resp.read()
                                    thumbnail_file = BufferedInputFile(thumb_bytes, filename="thumb.jpg")
                    except Exception as e:
                        logging.warning(f"Failed to download thumbnail: {e}")

                logging.info(f"Sending audio to dump chat {config.DUMP_CHAT_ID}")
                file = await bot.send_audio(
                    audio=audio_file,
                    title=data.title,
                    performer=str(data.artists),
                    thumbnail=thumbnail_file,
                    duration=data.duration,
                    chat_id=config.DUMP_CHAT_ID,
                )

                tg_file_id = file.audio.file_id
                db.save(track_id, tg_file_id)
                logging.info(f"Track saved to DB with file_id: {tg_file_id}")

            logging.info(f"Editing inline message media: {chosen_inline_result.inline_message_id}")
            await bot.edit_message_media(
                media=InputMediaAudio(
                    media=tg_file_id,
                    title=data.title,
                    performer=str(data.artists),
                    thumbnail=URLInputFile(data.cover_url),
                    duration=data.duration,
                ),
                inline_message_id=chosen_inline_result.inline_message_id,
            )
            await bot.edit_message_caption(
                inline_message_id=chosen_inline_result.inline_message_id,
                caption=f"<a href='{data.link}'>Yandex Music</a>\n<a href='{f'odesli.co/{data.link}'}'>song.link</a>",
                parse_mode="HTML",
            )
            logging.info("Successfully update inline message")
        except Exception as e:
            logging.exception(f"Error in chosen_track: {e}")
    else:
        logging.error(f"Unknown result id: {result_id}. Current result_ids: {list(result_ids.keys())}")
        # raise ("Unknown result id") - disabled to avoid crash


async def main() -> None:
    if config.PROXY_URL:
        logging.info(f"Using proxy: {config.PROXY_URL}")
        session = AiohttpSession(proxy=config.PROXY_URL)
        bot = Bot(token=TOKEN, session=session)
    else:
        logging.warning("No proxy configured, connecting directly")
        bot = Bot(token=TOKEN)

    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=_resolve_log_level(config.LOG_LEVEL), stream=sys.stdout)
    asyncio.run(main())
