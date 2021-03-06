# WaLLE
from ast import alias
import requests
import json
import discord
from discord.ext import commands
import random
import asyncio
import itertools
import sys
import traceback
from dotenv import load_dotenv
import os
from async_timeout import timeout
from functools import partial
import youtube_dl
from youtube_dl import YoutubeDL
import time
import datetime


load_dotenv()
# Get the API token from the .env file.
DISCORD_TOKEN = os.getenv('discord_token')

intents = discord.Intents().all()
client = discord.Client(intents=intents)
bot = commands.Bot(command_prefix='!', intents=intents)

bot.remove_command('help')


# Suppress noise about console usage from errors
youtube_dl.utils.bug_reports_message = lambda: ''

ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ffmpegopts = {
    'before_options': '-nostdin',
    'options': '-vn'
}

ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')
        self.duration = data.get('duration')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # take first item from a playlist
            # data = data['entries'][0]
            embed = discord.Embed(
                title="", description=f"Queued playlist [{data['title']}]({data['webpage_url']}) [{ctx.author.mention}]", color=discord.Color.green())
            await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title="", description=f"Queued [{data['title']}]({data['webpage_url']}) [{ctx.author.mention}]", color=discord.Color.green())
            await ctx.send(embed=embed)

        if download:
            source = ytdl.prepare_filename(data)
        else:
            if 'entries' in data:
                return data
            else:
                return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.
        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info,
                         url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url']), data=data, requester=requester)


class MusicPlayer:
    """A class which is assigned to each guild using the bot for Music.
    This class implements a queue and loop, which allows for different guilds to listen to different playlists
    simultaneously.
    When the bot disconnects from the Voice it's instance will be destroyed.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog',
                 'queue', 'next', 'current', 'np', 'volume', 'current_time')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None
        self.current_time = 0

        ctx.bot.loop.create_task(self.player_loop())

    # ANCHOR player_loop
    async def player_loop(self):
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'There was an error processing your song.\n'
                                             f'```css\n[{e}]\n```')
                    continue

            source.volume = self.volume
            self.current = source

            self.current_time = time.time()
            self._guild.voice_client.play(
                source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            embed = discord.Embed(
                title="Now playing", description=f"[{source.title}]({source.web_url}) [{source.requester.mention}]", color=discord.Color.green())
            self.np = await self._channel.send(embed=embed)
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to Voice Channel. '
                           'Please make sure you are in a valid channel or provide me with one')

        print('Ignoring exception in command {}:'.format(
            ctx.command), file=sys.stderr)
        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='join', aliases=['connect', 'j'], description="connects to voice")
    async def connect_(self, ctx, *, channel: discord.VoiceChannel = None):
        """Connect to voice.
        Parameters
        ------------
        channel: discord.VoiceChannel [Optional]
            The channel to connect to. If a channel is not specified, an attempt to join the voice channel you are in
            will be made.
        This command also handles moving the bot to different channels.
        """
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                embed = discord.Embed(
                    title="", description="No channel to join. Please call `,join` from a voice channel.", color=discord.Color.green())
                await ctx.send(embed=embed)
                raise InvalidVoiceChannel(
                    'No channel to join. Please either specify a valid channel or join one.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(
                    f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(
                    f'Connecting to channel: <{channel}> timed out.')
        if (random.randint(0, 1) == 0):
            await ctx.message.add_reaction('????')
        await ctx.send(f'**Joined `{channel}`**')

    # ANCHOR PLAY
    @commands.command(name='play', aliases=['sing', 'p'], description="streams music")
    async def play_(self, ctx, *, search: str):
        """Request a song and add it to the queue.
        This command attempts to join a valid voice channel if the bot is not already in one.
        Uses YTDL to automatically search and retrieve a song.
        Parameters
        ------------
        search: str [Required]
            The song to search and retrieve using YTDL. This could be a simple search, an ID or URL.
        """
        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        # If download is False, source will be a dict which will be used later to regather the stream.
        # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False)

        if 'entries' in source:
            # It's a playlist
            for song in source['entries']:
                src = {'webpage_url': song["webpage_url"],
                       'requester': ctx.author, 'title': song["title"]}
                await player.queue.put(src)
        else:
            await player.queue.put(source)

    # ANCHOR PAUSE
    @commands.command(name='pause', description="pauses music")
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            embed = discord.Embed(
                title="", description="I am currently not playing anything", color=discord.Color.green())
            return await ctx.send(embed=embed)
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send("Paused ??????")

    # ANCHOR RESUME
    @commands.command(name='resume', description="resumes music")
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(
                title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send("Resuming ??????")

    # ANCHOR SKIP
    @commands.command(name='skip', aliases=['next'], description="skips to next song in queue")
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(
                title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()

    # ANCHOR REMOVE
    @commands.command(name='remove', aliases=['rm', 'rem'], description="removes specified song from queue")
    async def remove_(self, ctx, pos: int = None):
        """Removes specified song from queue"""

        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(
                title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        if pos == None:
            player.queue._queue.pop()
        else:
            try:
                s = player.queue._queue[pos-1]
                del player.queue._queue[pos-1]
                embed = discord.Embed(
                    title="", description=f"Removed [{s['title']}]({s['webpage_url']}) [{s['requester'].mention}]", color=discord.Color.green())
                await ctx.send(embed=embed)
            except:
                embed = discord.Embed(
                    title="", description=f'Could not find a track for "{pos}"', color=discord.Color.green())
                await ctx.send(embed=embed)

    # ANCHOR CLEAR
    @commands.command(name='clear', aliases=['clr', 'cl', 'cr'], description="clears entire queue")
    async def clear_(self, ctx):
        """Deletes entire queue of upcoming songs."""

        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(
                title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        player.queue._queue.clear()
        await ctx.send('**Cleared**')

    # ANCHOR QUEUE
    @commands.command(name='queue', aliases=['q', 'playlist', 'que'], description="shows the queue")
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(
                title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        if player.queue.empty():
            embed = discord.Embed(
                title="", description="queue is empty", color=discord.Color.green())
            return await ctx.send(embed=embed)

        seconds = vc.source.duration % (24 * 3600)
        hour = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60
        if hour > 0:
            duration = "%dh %02dm %02ds" % (hour, minutes, seconds)
        else:
            duration = "%02dm %02ds" % (minutes, seconds)

        # Grabs the songs in the queue...
        upcoming = list(itertools.islice(player.queue._queue,
                        0, int(len(player.queue._queue))))
        fmt = '\n'.join(
            f"`{(upcoming.index(_)) + 1}.` [{_['title']}]({_['webpage_url']}) | ` {duration} Requested by: {_['requester']}`\n" for _ in upcoming)
        fmt = f"\n__Now Playing__:\n[{vc.source.title}]({vc.source.web_url}) | ` {duration} Requested by: {vc.source.requester}`\n\n__Up Next:__\n" + \
            fmt + f"\n**{len(upcoming)} songs in queue**"
        embed = discord.Embed(
            title=f'Queue for {ctx.guild.name}', description=fmt, color=discord.Color.green())
        embed.set_footer(text=f"{ctx.author.display_name}",
                         icon_url=ctx.author.avatar_url)

        await ctx.send(embed=embed)

    # ANCHOR NOW PLAYING
    @commands.command(name='np', aliases=['song', 'current', 'currentsong', 'playing'], description="shows the current playing song")
    async def now_playing_(self, ctx):
        """Display information about the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(
                title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)
        if not player.current:
            embed = discord.Embed(
                title="", description="I am currently not playing anything", color=discord.Color.green())
            return await ctx.send(embed=embed)

        seconds = vc.source.duration % (24 * 3600)

        time_played = time.time() - player.current_time
        progress_perc = round(time_played / seconds * 30)

        duration = datetime.timedelta(seconds=round(seconds))

        progress_bar = "??????????????????????????????????????????????????????????????????????????????????????????"[
            :progress_perc] + "???" + "??????????????????????????????????????????????????????????????????????????????????????????"[progress_perc+1:]

        time_now = datetime.timedelta(seconds=round(time_played))

        embed = discord.Embed(
            title="", description=f"[{vc.source.title}]({vc.source.web_url}) [{vc.source.requester.mention}] | `{duration}`", color=discord.Color.green())
        embed.set_author(icon_url=self.bot.user.avatar_url,
                         name=f"Now Playing ????")
        embed.add_field(
            # ???
            name="Progress", value=f"{progress_bar}", inline=False)
        embed.add_field(
            name="** **", value=f"???????????? ??????????????? ??????????????? ??? {time_now} / {duration} ??? ???????????? ????", inline=False)
        await ctx.send(embed=embed)

    # ANCHOR VOLUME
    @commands.command(name='volume', aliases=['vol', 'v'], description="changes Kermit's volume")
    async def change_volume(self, ctx, *, vol: float = None):
        """Change the player volume.
        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(
                title="", description="I am not currently connected to voice", color=discord.Color.green())
            return await ctx.send(embed=embed)

        if not vol:
            embed = discord.Embed(
                title="", description=f"???? **{(vc.source.volume)*100}%**", color=discord.Color.green())
            return await ctx.send(embed=embed)

        if not 0 < vol < 101:
            embed = discord.Embed(
                title="", description="Please enter a value between 1 and 100", color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        embed = discord.Embed(
            title="", description=f'**`{ctx.author}`** set the volume to **{vol}%**', color=discord.Color.green())
        await ctx.send(embed=embed)

    # ANCHOR LEAVE
    @commands.command(name='leave', aliases=["stop", "dc", "disconnect", "bye"], description="stops music and disconnects from voice")
    async def leave_(self, ctx):
        """Stop the currently playing song and destroy the player.
        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(
                title="", description="I'm not connected to a voice channel", color=discord.Color.green())
            return await ctx.send(embed=embed)

        if (random.randint(0, 1) == 0):
            await ctx.message.add_reaction('????')
        await ctx.send('**Successfully disconnected**')

        await self.cleanup(ctx.guild)


# ANCHOR CAT
@bot.command(name='cat', aliases=['kitty'], description="sends a random cat image")
async def cat_(ctx):
    """Send a random cat image."""
    r = requests.get('https://aws.random.cat/meow')

    embed = discord.Embed(
        title="MEOW", description="", color=discord.Color.green())
    embed.set_image(url=json.loads(r.text).get('file'))
    await ctx.send(embed=embed)


# ANCHOR MEME
@bot.command(name='meme', description="sends a random meme")
async def meme_(ctx):
    """Send a random meme"""

    if ctx.channel.id != 997092433047343114:
        embed = discord.Embed(
            title="Error!", description="This command is only available in the <#997092433047343114> channel", color=discord.Color.red())
        await ctx.send(embed=embed)
        return

    r = requests.get('https://meme-api.herokuapp.com/gimme/memes')

    embed = discord.Embed(
        title="MEME", description="", color=discord.Color.green())
    embed.set_image(url=json.loads(r.text).get('url'))
    await ctx.send(embed=embed)


# ANCHOR QUOTE
@bot.command(name='quote', aliases=['quotes'], description="sends a random quote")
async def quote_(ctx):
    """Send a random quote"""

    if ctx.channel.id != 997169441592840303:
        embed = discord.Embed(
            title="Error!", description="This command is only available in the <#997169441592840303> channel", color=discord.Color.red())
        await ctx.send(embed=embed)
        return

    r = requests.get('https://zenquotes.io/api/random')

    embed = discord.Embed(
        title="QUOTE", description=f"???{json.loads(r.text)[0].get('q')}??? ??? {json.loads(r.text)[0].get('a')} ", color=discord.Color.green())
    await ctx.send(embed=embed)


@bot.command(name='hack', description="calls hecker to hack someone")
@commands.has_permissions(manage_nicknames=True)
async def hecker_(ctx, *, user: discord.Member):
    """Calls hecker to hack someone"""

    embed = discord.Embed(title="Starting hacking",
                          description="hecker#8499", color=0x645034)
    embed.set_author(
        name="HECKER", icon_url="https://static.wikia.nocookie.net/beluga/images/9/9c/Hecker.jpg/revision/latest?cb=20210904163641")
    embed.add_field(name="HACKING PROGRESS",
                    value="|>         | 0%", inline=True)
    embed.set_footer(text="i'm always watching")
    msg = await ctx.send(embed=embed)
    await asyncio.sleep(0.5)

    for i in range(1, 11):
        embed = discord.Embed(title="Starting hacking",
                              description="hecker#8499", color=0x645034)
        embed.set_author(
            name="HECKER", icon_url="https://static.wikia.nocookie.net/beluga/images/9/9c/Hecker.jpg/revision/latest?cb=20210904163641")
        equals = "=" * i
        progress = i * 10
        embed.add_field(name="HACKING PROGRESS",
                        value=f"|{equals}>         | {progress}%", inline=True)
        embed.set_footer(text="i'm always watching")
        await msg.edit(embed=embed)
        await asyncio.sleep(0.5)

    await asyncio.sleep(0.5)
    embed = discord.Embed(title="Starting hacking",
                          description="hecker#8499", color=0x645034)
    embed.set_author(
        name="HECKER", icon_url="https://static.wikia.nocookie.net/beluga/images/9/9c/Hecker.jpg/revision/latest?cb=20210904163641")
    embed.add_field(name="HACKING PROGRESS",
                    value="|==========> | 100%", inline=True)
    embed.add_field(name="HACKING COMPLETE",
                    value=f"{user.mention} has been hacked", inline=False)
    embed.set_footer(text="i'm always watching")
    await ctx.send(embed=embed)
    await user.edit(nick="IM A BAD PERSON")


@bot.command(name='help', description="sends a help message")
async def help_(ctx):
    """Help message"""

    embed = discord.Embed(title="Help message",
                          description="You don't need help dumba$$. The commands are the same as all other servers -.-", color=0x645034)
    embed.set_author(
        name="A LITTLE KITTY CAT", icon_url="https://static.wikia.nocookie.net/beluga/images/9/9c/Hecker.jpg/revision/latest?cb=20210904163641")
    embed.set_footer(text="`dumba$$ again`")
    await ctx.send(embed=embed)


@client.event
async def on_member_join(member):
    channel = client.get_channel(997095041241731152)
    embed = discord.Embed(title="Welcome to the server",
                          description="Welcome to the server little chicken nuggets muffin with pieces of bananas on it", color=0x645034)
    embed.set_author(
        name="A LITTLE KITTY CAT", icon_url="https://static.wikia.nocookie.net/beluga/images/9/9c/Hecker.jpg/revision/latest?cb=20210904163641")
    embed.add_field(name="COOKIE", value="????", inline=True)
    embed.add_field(name="BANANA", value="????", inline=True)
    embed.add_field(name="TERMS AND CONDITIONS",
                    value="IF YOU DONT GET A COOKIE WE WILL SLAP YOUR A$$ :D", inline=False)
    await channel.send(embed=embed)


@bot.event
async def on_ready():
    for guild in bot.guilds:
        print('Active in {}\n Member Count : {}'.format(
            guild.name, guild.member_count))


def setup(bot):
    bot.add_cog(Music(bot))


if __name__ == "__main__":
    setup(bot)
    bot.run(DISCORD_TOKEN)
