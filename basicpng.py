# Copyright (c) 2024, Vlad Meșco
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
# 
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
Basic PNG decoder, for when you are not allowed to depend on cv or pillow
or any other useful Python/C image library.

PngDecode supports the basic IHDR, IEND, IDAT, PLTE blocks deemed "critical"
for PNG rendering, and none of the extensions.

PngDecode does not support Adam7 interlaced images, only linear scan ones.

PngDecode.get allows you to retrieve RGBA U8 pixels regardless of the physical
storage. This is normally fine, unless you really wanted to read 48bit images
at their full fidelity. Sorry, but you get them downsampled to 24bit.
"""
import zlib
import os
import math

def PaethPredictor(a, b, c):
    """
    Paeth filter. This is the exact implementation from the PNG spec.
    It must be this exact implementation, otherwise it will decode wrong.

    Arithmetics are done in Z (i.e. p may be <0 or >255).

    We go back to module 256 in the actual PNG decompression stage.
    """
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    elif pb <= pc:
        return b
    else:
        return c

def Clamp(x):
    """
    Ensures x is within 0 and 255, and that it is an int()
    """
    x = int(round(x))
    if x >= 0 and x <= 255:
        return x
    elif x < 0:
        return 0
    else:
        return 255

def GetNormalizer(bits_per_channel, is_indexed):
    """
    Returns a lambda which normalizes a number of < 8 bits wide to fill the
    0..255 range as if it were a full byte.
    """
    if is_indexed:
        return lambda x: x
    else:
        return lambda x: Clamp(x / float((1 << bits_per_channel) - 1) * 255.0)

def ExplodeBytes(data, bits_per_channel, is_indexed):
    """
    Given some pixel channel bytes, transforms them to 8 bits per channel.

    16bit data is downsampled to 8bit.

    1, 2 and 4 bit data is expanded and scaled to 8 bit.

    If is_indexed is False, the byte data is scaled to fill the range 0..255.
    If is_indexed is False, the data is left untouched, as it will be an index
    in the PLTE block.
    """
    if bits_per_channel == 8:
        return data

    rval = bytearray([])
    if bits_per_channel == 16:
        for i in range(0, len(data) / 2):
            # Use the lower byte to determine if we need to round up
            b2 = data[2*i+1]
            # But mostly use the high byte
            b1 = data[2*i+0] if b2 < 128 else Clamp(data[2*i+0] + 1)
            rval.append(b1)
    elif bits_per_channel == 4:
        normalizer = GetNormalizer(is_indexed, 4)
        for i in range(0, len(data)):
            bb = data[i]
            rval.append(normalizer((bb & 0xF0) >> 4))
            rval.append(normalizer((bb & 0x0F) >> 0))
    elif bits_per_channel == 2:
        normalizer = GetNormalizer(is_indexed, 2)
        for i in range(0, len(data)):
            bb = data[i]
            rval.append(normalizer((bb & 0xC0) >> 6))
            rval.append(normalizer((bb & 0x30) >> 4))
            rval.append(normalizer((bb & 0x0C) >> 2))
            rval.append(normalizer((bb & 0x03) >> 0))
    elif bits_per_channel == 1:
        normalizer = GetNormalizer(is_indexed, 1)
        for i in range(0, len(data)):
            bb = data[i]
            rval.append(normalizer((bb & 0x80) >> 7))
            rval.append(normalizer((bb & 0x40) >> 6))
            rval.append(normalizer((bb & 0x20) >> 5))
            rval.append(normalizer((bb & 0x10) >> 4))
            rval.append(normalizer((bb & 0x08) >> 3))
            rval.append(normalizer((bb & 0x04) >> 2))
            rval.append(normalizer((bb & 0x02) >> 1))
            rval.append(normalizer((bb & 0x01) >> 0))
    return rval

class EndReading(Exception):
    """
    Used by PngDecode to tell itself when the PNG stream says it ended.
    """
    def __init__(self):
        Exception.__init__(self, "Should exit PngDecode.parse")

class PngDecode:
    """
    Decode basic PNGs. Only parses IHDR, IDAT, PLTE and IEND.
    Does not support fancy extensions.
    Does not support Adam7 interlacing.

    When done, the get(x,y) method yields an RGBA U8 tuple.

    R and RA formats become grayscale.

    16 bits per channel formats get downsampled to 8.
    """
    def __init__(self, fname):
        self.w = 0
        self.h = 0
        # see IHDR; this dictates the amount of channels
        self.color_type = 0
        self.num_channels = 0
        self.bit_depth = 0
        # there is only one
        self.compression = 0
        # decoded bytes
        self.rgba = bytearray([])
        # all IDAT chunks, which are one long deflate stream
        self.idat_data = bytearray([])
        # PLTE table
        self.palette = None
        #print(f"Parsing {fname}")
        self.parse(fname)
        #print(f"Done parsing {fname}")

        if self.color_type == 3 and self.palette is None:
            raise Exception(f"{fname} uses indexed color, but there is not PLTE chunk in the stream!")

    def IHDR(self, bchd):
        """
        Fixed size header, 13 bytes, 7 numbers.

        All ints in PNG are in network byte order, which is a fancy way
        to say "Big Endian"
        """
        #print("Parsing IHDR")
        self.w = int.from_bytes(bchd[0:4], "big")
        self.h = int.from_bytes(bchd[4:8], "big")
        self.bit_depth = bchd[8]
        self.color_type = bchd[9]
        self.compression = bchd[10]
        self.filter = bchd[11]
        self.interlace = bchd[12]
        # color types:              bit depths
        # 0: R                      1,2,4,8,16
        # 2: RGB                    8,16
        # 3: PLTE index             1,2,4,8
        # 4: RA                     8,16
        # 6: RGBA                   8,16
        # 1 and 5 are missing because they are not in the spec. Let it crash on garbage input
        CHANNELS = {
            0: 1,
            2: 3,
            3: 1,
            4: 2,
            6: 4
        }
        self.num_channels = CHANNELS[self.color_type]
        if self.compression != 0:
            # at the time of writing, there is only one (DEFLATE)
            raise Exception("Only DEFLATE compression (method 0) is supported!")
        if self.filter != 0:
            # at the time of writing, there is only one (and only)
            raise Exception("Only filter method 0 is supported!")
        if self.interlace != 0:
            # I really, really hope we don't have to deal with  interlaced images any time soon.
            # Adam7 is useful since it allows you to read 1/64th of the file and it's enough to get 1/64th of the image as a thumbnail; the subsequent 6 passes fill in the missing pixels, getting you to half, then 3/4, then full image.
            # It's really fancy to compute thumbnails out of it, it's not trivial to write, it's really (really) annoying to actually read.
            # Might as well switch to libImageMagick at that point.
            # Adam7 is the only interlacing method, apart from "None"
            raise Exception(f"Only interlace method 0 (no interlace) is supported! Adam7 is not, found {self.interlace}")

        #print(f"w={self.w} h={self.h} bit_depth={self.bit_depth} num_channels={self.num_channels} color_type={self.color_type}")

    def IDAT(self, bchd):
        """
        All IDAT chunks refer to the (one and only) Image Data, so we
        need to fetch all of them, since it's one big DEFLATE stream
        """
        #print("Saving IDAT")
        self.idat_data.extend(bchd)

    def PLTE(self, bchd):
        """
        Parse PLTE chunk. Allegedly this must precede IDAT, but we don't
        care to enforce that rule.
        """
        sz = len(bchd) // 3
        if sz == 0 or sz > 255:
            return
        # The value from IDAT will index this array directly
        self.palette = []
        for i in range(0, sz):
            self.palette.append([bchd[3*i+0], bchd[3*i+1], bchd[3*i+2]])

    def decompress(self):
        #print("Parsing all IDAT chunks")
        # PNG implicitly specifies DEFLATE + 32767, so let's create an object; the defaults to decompressobj are 2^15 and no custom dictionary, which will do
        zlibd = zlib.decompressobj()
        ddat = zlibd.decompress(self.idat_data)

        #print(f"   len(ddat)={len(ddat)}")

        # h rows
        # rows have a filter byte (since there is only one filter method in existance, and this is its spec) and w * bit_depth * num_channels subpixels
        # Note, alpha may be encoded as a magic color specified by a magic CHUNK; we don't care about that
        num_channels = self.num_channels
        # assumed to be an integer multiple of a byte
        bit_depth = self.bit_depth
        is_indexed = self.color_type == 3
        #www = self.w * num_channels + 1
        www = int((self.w * num_channels * bit_depth / 8) + 1)
        for j in range(0, self.h):
            # grab a scaline
            brow = ddat[j * www:(j+1) * www]
            # figure out how the heck it's encoded.
            # The way they did this is pretty smart, as it gets solids and gradients to result in values clustered around 0, making DEFLATE much more efficient!
            filter_subtype = brow[0]
            #print(f"    Row {j+1}/{self.h}: {filter_subtype}")
            if filter_subtype == 0:
                # None. Just push the bytes in the output array
                #print(f"    Row {j+1}/{self.h}: No filtering")
                self.rgba.extend(ExplodeBytes(brow[1:], bit_depth, is_indexed))
            elif filter_subtype == 1:
                # Sub. The byte was encoded as a difference from the byte to the left
                # All nonexisting bytes are considered 0.
                # Note: none of the filters care about bits per channel, they all work on 8bit bytes
                #print(f"    Row {j+1}/{self.h}: Sub")
                bpp = self.num_channels
                buffer = bytearray([0, 0, 0, 0, 0, 0, 0, 0]) + ExplodeBytes(brow[1:], bit_depth, is_indexed)
                for i in range(0, len(brow)-1):
                    decoded = ( buffer[i+8] + buffer[i+8-bpp] )%256
                    buffer[i+8] = decoded
                    self.rgba.append(decoded)
            elif filter_subtype == 2:
                # Up. The byte was encoded as a difference from the byte above
                #print(f"    Row {j+1}/{self.h}: Up")
                prior = self.rgba[-(self.w * self.num_channels):]
                buffer = ExplodeBytes(brow[1:], bit_depth, is_indexed)
                if len(prior) == 0:
                    prior = bytes([0] * len(brow)-1)
                for i in range(0, len(brow)-1):
                    decoded = ( buffer[i] + prior[i] )%256
                    self.rgba.append(decoded)
            elif filter_subtype == 3:
                # Average. The byte was encoded as a difference from the average of the byte to the left and the byte above
                #print(f"    Row {j+1}/{self.h}: Average")
                bpp = self.num_channels
                prior = bytes([0, 0, 0, 0, 0, 0, 0, 0]) + self.rgba[-(self.w * self.num_channels):]
                if len(prior) == 0:
                    prior = bytes([0] * (8+len(brow)-1))
                buffer = bytearray([0, 0, 0, 0, 0, 0, 0, 0]) + ExplodeBytes(brow[1:], bit_depth, is_indexed)
                for i in range(0, len(brow)-1):
                    decoded = ( buffer[i+8] + int(math.floor((buffer[i+8-bpp]+prior[i+8])/2)) )%256
                    buffer[i+8] = decoded
                    self.rgba.append(decoded)
            elif filter_subtype == 4:
                # Paeth predictor used to encode byte. Google it.
                # We just need to add the prediction back to decode our byte.
                # This muxes the bytes to the left, above, and up-left
                #print(f"    Row {j+1}/{self.h}: Paeth")
                bpp = self.num_channels
                prior = bytes([0, 0, 0, 0, 0, 0, 0, 0]) + self.rgba[-(self.w * self.num_channels):]
                if len(prior) == 0:
                    prior = bytes([0] * (8+len(brow)-1))
                buffer = bytearray([0, 0, 0, 0, 0, 0, 0, 0]) + ExplodeBytes(brow[1:], bit_depth, is_indexed)
                for i in range(0, len(brow)-1):
                    decoded = ( buffer[i+8] + PaethPredictor(buffer[i+8-bpp], prior[i+8], prior[i+8-bpp]) )%256
                    buffer[i+8] = decoded
                    self.rgba.append(decoded)
            else:
                raise Exception(f"Non standard filter type {filter_subtype}")

    def IEND(self, bchd):
        """IEND appears last, and marks the end of the PNG stream"""
        #print("Parsing IEND")
        raise EndReading()

    def parse(self, fname):
        with open(fname, "rb") as f:
            # Header is always .PNG.... ; per Spec.
            # The first byte marks issues with the MBS being reset.
            # Together with the next three bytes detect issues with
            # endianness, and mark the file as a PNG file.
            # The remaining four bytes detect issues with omitting
            # whitespace, or of \r\n and \n being converted form one
            # to another. Yes, this is the definition of magic bytes :-)
            hdr = f.read(8)
            if hdr != bytes([137, 80, 78, 71, 13, 10, 26, 10]):
                raise Exception(f"{fname} corrupt or not a png?")

            try:
                # read until we have a reason to exit
                while True:
                    # Each chunk is u32be size, u8[4] id, u8[size] data, u32be crc32
                    # The id uses bit 5 as special flags which I don't care about.
                    # If you don't know your ASCII, bit 5 is what makes letters uppercase.

                    # chunk length                    
                    blen = f.read(4)
                    if len(blen) < 4:
                        # EOF probably
                        #print("Nothing more to read, exiting")
                        raise EndReading()
                    dlen = int.from_bytes(blen, "big")
                    # chunk name / id / type
                    # first byte, bit 5: unset = critical for display
                    # second byte, bit 5: set = application private chunk, irrelevant
                    # third byte, bit 5: reserved, irrelevant
                    # fourth byte, bit 5: of relevance only to PNG editors, so... irrelevant
                    # You can see that we ignore some critical chunks, such as PLTE (indexed color)
                    bcht = f.read(4)
                    scht = bcht.decode('utf-8')
                    # Chunk data. dlen bytes
                    bchd = f.read(dlen) if dlen > 0 else bytes([])
                    # CRC32
                    # Don't care about CRC, assume file got "transmitted correctly"
                    bcrc = f.read(4)

                    #print(f"Found {scht} len={dlen}")

                    # If we have a handler for a chunk type, handle it.
                    if scht.upper() in dir(self):
                        getattr(self, scht.upper())(bchd)
            except EndReading:
                # pat ourselves on the back for getting through to the
                # end of the file, and start decoding the IDAT stream
                self.decompress()

    def get(self, x, y):
        """
        Retrieve the RGBA8 values at x x y.

        Returns tuple(r, g, b, a)

        Check this.w and this.h for size
        """
        ww = self.num_channels
        www = self.w * ww
        start = y * www + x * ww
        pixel = self.rgba[start:start+self.num_channels]

        # if indexed, grab the RGB values from the palette
        if self.color_type == 3:
            pixel = self.palette[pixel[0]]

        if len(pixel) == 1:
            # assume they meant grayscale
            pixel.append(pixel[0])
            pixel.append(pixel[0])
            pixel.append(255)
        elif len(pixel) == 2:
            # Only the RA format yields 2 channels
            pixel = bytearray([pixel[0], pixel[0], pixel[0], pixel[1]])
        elif len(pixel) == 3:
            # Only the RGB format yields 3 channels
            pixel.append(255)
            # Note, there could have been a whole bunch of ancillary chunks
            # concerning keyed transparency, gamma, etc, which we have promptly ignored.
        # RGBA yields 4 channels; the others were appropriately padded
        return (pixel[0], pixel[1], pixel[2], pixel[3])
