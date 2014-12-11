"""Classes relating to BlockingStrategies, which define ways to carve up Images into Blocks.
"""
import itertools
from numpy import expand_dims, zeros

from thunder.rdds.imgblocks.blocks import SimpleBlocks, BlockGroupingKey, Blocks


class BlockingStrategy(object):
    """Superclass for objects that define ways to split up images into smaller blocks.
    """
    def __init__(self):
        self._dims = None
        self._nimages = None
        self._dtype = None

    @property
    def dims(self):
        """Shape of the Images data to which this BlockingStrategy is to be applied.

        dims will be taken from the Images passed in the last call to setImages().

        n-tuple of positive int, or None if setImages has not been called
        """
        return self._dims

    @property
    def nimages(self):
        """Number of images (time points) in the Images data to which this BlockingStrategy is to be applied.

        nimages will be taken from the Images passed in the last call to setImages().

        positive int, or None if setImages has not been called
        """
        return self._nimages

    @property
    def dtype(self):
        """Numpy data type of the Images data to which this BlockingStrategy is to be applied.

        String or numpy dtype, or None if setImages has not been called
        """
        return self.dtype

    def setImages(self, images):
        """Readies the BlockingStrategy to operate over the passed Images object.

        dims, nimages, and dtype will be initialized by this call.

        No return value.
        """
        self._dims = images.dims
        self._nimages = images.nimages
        self._dtype = images.dtype

    def getBlocksClass(self):
        """Get the subtype of Blocks that instances of this strategy will produce.

        Subclasses should override this method to return the appropriate Blocks subclass.
        """
        return Blocks

    def blockingFunction(self, timePointIdxAndImageArray):
        raise NotImplementedError("blockingFunction not implemented")

    def combiningFunction(self, spatialIdxAndBlocksSequence):
        raise NotImplementedError("combiningFunction not implemented")


class SimpleBlockingStrategy(BlockingStrategy):
    """A BlockingStrategy that groups Images into nonoverlapping, roughly equally-sized blocks.

    The number and dimensions of image blocks are specified as "splits per dimension", which is for each
    spatial dimension of the original Images the number of partitions to generate along that dimension. So
    for instance, given a 12 x 12 Images object, a SimpleBlockingStrategy with splitsPerDim=(2,2)
    would yield Blocks objects with 4 blocks, each 6 x 6.
    """
    def __init__(self, splitsPerDim):
        """Returns a new SimpleBlockingStrategy.

        Parameters
        ----------
        splitsPerDim : n-tuple of positive int, where n = dimensionality of image
            Specifies that intermediate blocks are to be generated by splitting the i-th dimension
            of the image into splitsPerDim[i] roughly equally-sized partitions.
            1 <= splitsPerDim[i] <= self.dims[i]
        """
        super(SimpleBlockingStrategy, self).__init__()
        self._splitsPerDim = SimpleBlockingStrategy.__normalizeSplits(splitsPerDim)
        self._slices = None

    def getBlocksClass(self):
        return SimpleBlocks

    @classmethod
    def generateFromBlockSize(cls, blockSize, dims, nimages, datatype, **kwargs):
        """Returns a new SimpleBlockingStrategy, that yields blocks
        closely matching the requested size in bytes.

        Parameters
        ----------
        blockSize : positive int or string
            Requests an average size for the intermediate blocks in bytes. A passed string should
            be in a format like "256k" or "150M" (see util.common.parseMemoryString). If blocksPerDim
            or groupingDim are passed, they will take precedence over this argument. See
            strategy._BlockMemoryAsSequence for a description of the blocking strategy used.

        Returns
        -------
        n-tuple of positive int, where n == len(self.dims)
            Each value in the returned tuple represents the number of splits to apply along the
            corresponding dimension in order to yield blocks close to the requested size.
        """
        import bisect
        from numpy import dtype
        from thunder.utils.common import parseMemoryString
        minseriessize = nimages * dtype(datatype).itemsize

        if isinstance(blockSize, basestring):
            blockSize = parseMemoryString(blockSize)

        memseq = _BlockMemoryAsReversedSequence(dims)
        tmpidx = bisect.bisect_left(memseq, blockSize / float(minseriessize))
        if tmpidx == len(memseq):
            # handle case where requested block is bigger than the biggest image
            # we can produce; just give back the biggest block size
            tmpidx -= 1
        splitsPerDim = memseq.indtosub(tmpidx)
        return cls(splitsPerDim, **kwargs)

    @classmethod
    def generateForImagesFromBlockSize(cls, images, blockSize, **kwargs):
        """Returns a new SimpleBlockingStrategy, that yields blocks
        closely matching the requested size in bytes.
        """
        strategy = cls.generateFromBlockSize(blockSize, images.dims, images.nimages, images.dtype, **kwargs)
        strategy.setImages(images)
        return strategy

    @staticmethod
    def __normalizeSplits(splitsPerDim):
        splitsPerDim = map(int, splitsPerDim)
        if any((nsplits <= 0 for nsplits in splitsPerDim)):
            raise ValueError("All numbers of blocks must be positive; got " + str(splitsPerDim))
        return splitsPerDim

    def __validateSplitsForImage(self):
        dims = self.dims
        splitsPerDim = self._splitsPerDim
        ndim = len(dims)
        if not len(splitsPerDim) == ndim:
            raise ValueError("splitsPerDim length (%d) must match image dimensionality (%d); " %
                             (len(splitsPerDim), ndim) +
                             "have splitsPerDim %s and image shape %s" % (str(splitsPerDim), str(dims)))

    @staticmethod
    def __generateSlices(splitsPerDim, dims):
        # slices will be sequence of sequences of slices
        # slices[i] will hold slices for ith dimension
        slices = []
        for nsplits, dimsize in zip(splitsPerDim, dims):
            blocksize = dimsize / nsplits  # integer division
            blockrem = dimsize % nsplits
            st = 0
            dimslices = []
            for blockidx in xrange(nsplits):
                en = st + blocksize
                if blockrem:
                    en += 1
                    blockrem -= 1
                dimslices.append(slice(st, min(en, dimsize), 1))
                st = en
            slices.append(dimslices)
        return slices

    def setImages(self, images):
        super(SimpleBlockingStrategy, self).setImages(images)
        self.__validateSplitsForImage()
        self._slices = SimpleBlockingStrategy.__generateSlices(self._splitsPerDim, self.dims)

    @staticmethod
    def extractBlockFromImage(imgary, blockslices, timepoint, numtimepoints):
        # add additional "time" dimension onto front of val
        val = expand_dims(imgary[blockslices], axis=0)
        origshape = [numtimepoints] + list(imgary.shape)
        origslices = [slice(timepoint, timepoint+1, 1)] + list(blockslices)
        return BlockGroupingKey(origshape, origslices), val

    def blockingFunction(self, timePointIdxAndImageArray):
        tpidx, imgary = timePointIdxAndImageArray
        totnumimages = self.nimages
        slices = self._slices

        ret_vals = []
        sliceproduct = itertools.product(*slices)
        for blockslices in sliceproduct:
            ret_vals.append(SimpleBlockingStrategy.extractBlockFromImage(imgary, blockslices, tpidx, totnumimages))
        return ret_vals

    def combiningFunction(self, spatialIdxAndBlocksSequence):
        _, partitionedSequence = spatialIdxAndBlocksSequence
        # sequence will be of (partitioning key, np array) pairs
        ary = None
        firstkey = None
        for key, block in partitionedSequence:
            if ary is None:
                # set up collection array:
                newshape = [key.origshape[0]] + list(block.shape)[1:]
                ary = zeros(newshape, block.dtype)
                firstkey = key

            # put values into collection array:
            targslices = [key.origslices[0]] + ([slice(None)] * (block.ndim - 1))
            ary[targslices] = block

        # new slices should be full slice for formerly planar dimension, plus existing block slices
        neworigslices = [slice(None)] + list(firstkey.origslices)[1:]
        return BlockGroupingKey(origshape=firstkey.origshape, origslices=neworigslices), ary


class _BlockMemoryAsSequence(object):
    """Helper class used in calculation of slices for requested blocks of a particular size.

    The blocking strategy represented by objects of this class is to split into N equally-sized
    subdivisions along each dimension, starting with the rightmost dimension.

    So for instance consider an Image with spatial dimensions 5, 10, 3 in x, y, z. The first nontrivial
    subdivision would be to split into 2 blocks along the z axis:
    splits: (1, 1, 2)
    In this example, downstream this would turn into two blocks, one of size (5, 10, 2) and another
    of size (5, 10, 1).

    The next subdivision would be to split into 3 blocks along the z axis, which happens to
    corresponding to having a single block per z-plane:
    splits: (1, 1, 3)
    Here these splits would yield 3 blocks, each of size (5, 10, 1).

    After this the z-axis cannot be split further, so the next subdivision starts splitting along
    the y-axis:
    splits: (1, 2, 3)
    This yields 6 blocks, each of size (5, 5, 1).

    Several other splits are possible along the y-axis, going from (1, 2, 3) up to (1, 10, 3).
    Following this we move on to the x-axis, starting with splits (2, 10, 3) and going up to
    (5, 10, 3), which is the finest subdivision possible for this data.

    Instances of this class represent the average size of a block yielded by this blocking
    strategy in a linear order, moving from the most coarse subdivision (1, 1, 1) to the finest
    (x, y, z), where (x, y, z) are the dimensions of the array being partitioned.

    This representation is intended to support binary search for the blocking strategy yielding
    a block size closest to a requested amount.
    """
    def __init__(self, dims):
        self._dims = dims

    def indtosub(self, idx):
        """Converts a linear index to a corresponding blocking strategy, represented as
        number of splits along each dimension.
        """
        dims = self._dims
        ndims = len(dims)
        sub = [1] * ndims
        for didx, d in enumerate(dims[::-1]):
            didx = ndims - (didx + 1)
            delta = min(dims[didx]-1, idx)
            if delta > 0:
                sub[didx] += delta
                idx -= delta
            if idx <= 0:
                break
        return tuple(sub)

    def blockMemoryForSplits(self, sub):
        """Returns the average number of cells in a block generated by the passed sequence of splits.
        """
        from operator import mul
        sz = [d / float(s) for (d, s) in zip(self._dims, sub)]
        return reduce(mul, sz)

    def __len__(self):
        return sum([d-1 for d in self._dims]) + 1

    def __getitem__(self, item):
        sub = self.indtosub(item)
        return self.blockMemoryForSplits(sub)


class _BlockMemoryAsReversedSequence(_BlockMemoryAsSequence):
    """A version of _BlockMemoryAsSequence that represents the linear ordering of splits in the
    opposite order, starting with the finest blocking scheme allowable for the array dimensions.

    This can yield a sequence of block sizes in increasing order, which is required for binary
    search using python's 'bisect' library.
    """
    def _reverseIdx(self, idx):
        l = len(self)
        if idx < 0 or idx >= l:
            raise IndexError("list index out of range")
        return l - (idx + 1)

    def indtosub(self, idx):
        return super(_BlockMemoryAsReversedSequence, self).indtosub(self._reverseIdx(idx))
