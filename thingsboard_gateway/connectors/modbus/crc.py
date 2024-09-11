modbus_crc16 = {
    'init_data': 0xffff,
    'poly': 0x8005,
    'size': 8,
    'in_reverse': False,
    'out_reverse': False
}


def reverse(ds, size):
    bin_data = bin(ds)
    bin_data_str = str(bin_data)
    out_bin_str = ''
    for i in range(size):
        if i < len(bin_data_str) - 2:
            out_bin_str = out_bin_str + (bin_data_str[len(bin_data_str) - i - 1])
        else:
            out_bin_str = out_bin_str + '0'
    out = int(out_bin_str, 2)
    return out


def calculator(ds, cfg):
    init_data = cfg['init_data']
    for i in range(0, int(len(ds) / 2)):
        d = ds[i * 2:i * 2 + 2]
        d10 = int(d, 16)
        if cfg['in_reverse']:
            d10 = reverse(d10, cfg['size'])
        init_data = d10 ^ init_data
        for i in range(cfg['size']):
            if 1 & init_data == 1:
                init_data = init_data >> 1
                init_data = init_data ^ reverse(cfg['poly'], 16)
            else:
                init_data = init_data >> 1
    if cfg['out_reverse']:
        out = hex(reverse(init_data, 16))
    else:
        out = hex(init_data)
    return out
