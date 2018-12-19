CUDA_VISIBLE_DEVICES=0 time python ./tools/test_net.py \
    --imdb Tvoc_2007_test \
    --model  \
    --cfg experiments/cfgs/${NET}.yml \
    --tag ${EXTRA_ARGS} \
    --net ${NET} \
    --set ANCHOR_SCALES ${ANCHORS} ANCHOR_RATIOS ${RATIOS} \


